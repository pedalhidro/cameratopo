"""Exporta um TIER de resolução reduzida do FABDEM pelo Earth Engine.

O mosaico FABDEM (1°×1°, 30 m) tem teto de COGs por tile no render.py
(MOSAIC_MAX_*): zoom afastado = dezenas/centenas de arquivos por tile → caro
ou vazio. O tier resolve isso com UM grid global de baixa resolução (poucos
arquivos grandes, com overviews), lido pelo render.py nos zooms afastados.

Duas bandas int16 (nodata −32768), no grid EPSG:4326 de passo 1/PPD grau:
  elev   — MÉDIA da elevação nativa (m, arredondada)
  slope  — MÉDIA da declividade calculada NA RESOLUÇÃO NATIVA (tan, ×10000)
A declividade sai do grid nativo e só DEPOIS é agregada (reduceResolution
mean) — como o app GEE faz na pirâmide e como exige o invariante do
render.py ("declividade derivada ~na resolução nativa, sempre"). Derivar
declividade do grid de 500 m daria um relevo ~achatado e sem textura.

Uso (auth: token do gcloud do usuário, vale ~1 h — a tarefa roda no Google):
    T=$(gcloud auth print-access-token) python tools/export_fabdem_tier.py 240
    #   240 px/grau (≈ 463 m), globo, arquivos de 90° → gs://telhas/dem/fabdem_500m/
    T=... python tools/export_fabdem_tier.py 1200 fabdem_90m_sa -90,-60,-30,20 10
    #   1200 px/grau (≈ 92 m), América do Sul, arquivos de 10° → …/fabdem_90m_sa/

LACUNA (Armênia/Azerbaijão): o FABDEM do EE não tem esses países (o GLO-30
público exclui os dois), mas o bucket do R2 tem COP30 no mesmo grid — seção
"# gap-cop30" do fabdem_cells.txt. O EE não lê o R2: copie as células pro GCS
e passe o prefixo em GAP (sem GAP, o tier sai com a lacuna como mar, 0 m):
    for c in $(sed -n '/^# gap-cop30/,$p' fabdem_cells.txt | grep -v '^#'); do
      curl -s https://fabdem.pedalhidrografi.co/${c}_FABDEM_V1-2.tif \
        | gcloud storage cp - gs://telhas/dem/cop30_gap/${c}_FABDEM_V1-2.tif; done
    GAP=gs://telhas/dem/cop30_gap T=... python tools/export_fabdem_tier.py 240

args: ppd [nome] [oeste,sul,leste,norte] [lado do arquivo em graus]. A região
tem que ser múltipla do lado do arquivo: a origem do grid É o canto NO da
região, e o EE nomeia cada arquivo <prefixo><linha px>-<coluna px>.tif (10
dígitos, SEM hífen depois do prefixo) a partir dela — o render.py deriva os limites pelos offsets (TIERS).
"""

import math
import os
import sys

import ee
import google.oauth2.credentials

FABDEM = "projects/sat-io/open-datasets/FABDEM"
BUCKET = "telhas"
NODATA = -32768


def gap_cells():
    """Células da seção '# gap-cop30' do fabdem_cells.txt (tudo depois do marcador)."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fabdem_cells.txt")
    out, on = [], False
    for line in open(p):
        c = line.strip()
        if c.startswith("# gap-cop30"):
            on = True
        elif on and c and not c.startswith("#"):
            out.append(c)
    return out


def main(ppd: int, name: str, region: tuple, file_deg: float):
    token = os.environ.get("T")
    if token:
        ee.Initialize(credentials=google.oauth2.credentials.Credentials(token),
                      project="pedal-hidrografico")
    else:
        ee.Initialize(project="pedal-hidrografico")

    west, south, east, north = region
    file_px = int(round(file_deg * ppd))
    # bloco de cálculo do EE: fileDimensions tem que ser múltiplo dele, e bloco
    # grande estoura memória ("computation too large") — 240 divide 90°·240 e 10°·1200
    shard = 240 if file_px % 240 == 0 else 256

    col = ee.ImageCollection(FABDEM)
    native = col.first().select(0).projection()
    gap = os.environ.get("GAP")
    if gap:   # lacuna do FABDEM preenchida com os COP30 do GCS (mesmo grid 1")
        cells = gap_cells()
        imgs = [ee.Image.loadGeoTIFF(f"{gap.rstrip('/')}/{c}_FABDEM_V1-2.tif") for c in cells]
        imgs = [i.select([0], ["b1"]).updateMask(i.select(0).neq(-9999)) for i in imgs]
        col = col.select([0], ["b1"]).merge(ee.ImageCollection(imgs))
        print("lacuna:", len(cells), "células de", gap)
    elev = col.mosaic().setDefaultProjection(native)          # grid NATIVO (1")
    slope_tan = ee.Terrain.slope(elev).multiply(math.pi / 180.0).tan()
    stack = elev.addBands(slope_tan).rename(["elev", "slope"])

    step = 1.0 / ppd
    transform = [step, 0, float(west), 0, -step, float(north)]   # origem = canto NO
    # (step·3600)² células nativas por pixel de saída (240 ppd → 225); folga 4×
    max_px = int(math.ceil((3600 / ppd) ** 2 * 4))
    agg = (stack.reduceResolution(reducer=ee.Reducer.mean(), maxPixels=max_px)
           .reproject(crs="EPSG:4326", crsTransform=transform))
    out = ee.Image.cat(
        agg.select("elev").round().toInt16(),
        agg.select("slope").multiply(10000).round().min(32767).toInt16(),
    )

    task = ee.batch.Export.image.toCloudStorage(
        image=out, description=name, bucket=BUCKET,
        fileNamePrefix=f"dem/{name}/{name}",
        region=ee.Geometry.Rectangle([west, south, east, north], None, False),
        crs="EPSG:4326", crsTransform=transform,
        maxPixels=int(1e11), shardSize=shard, fileDimensions=[file_px, file_px],
        fileFormat="GeoTIFF",
        formatOptions={"cloudOptimized": True, "noData": NODATA},
    )
    task.start()
    print(name, "→", f"gs://{BUCKET}/dem/{name}/", "task", task.id)


if __name__ == "__main__":
    a = sys.argv[1:]
    ppd = int(a[0]) if a else 240
    name = a[1] if len(a) > 1 else "fabdem_500m"
    region = tuple(float(v) for v in a[2].split(",")) if len(a) > 2 else (-180, -90, 180, 90)
    file_deg = float(a[3]) if len(a) > 3 else 90.0
    main(ppd, name, region, file_deg)
