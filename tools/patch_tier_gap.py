"""Remendo do tier de 500 m SÓ na lacuna do FABDEM (Armênia/Azerbaijão).

O tier `fabdem_500m` saiu do Earth Engine, cujo FABDEM não tem esses países
(o GLO-30 público exclui os dois). Reexportar o globo inteiro por 25 células
não vale: este script calcula, LOCALMENTE e direto dos COGs do R2 (os COP30 da
seção "# gap-cop30" do fabdem_cells.txt), um arquivo pequeno no MESMO grid do
tier — origem (−180, 90), passo 1/240° — com dado só nas células da lacuna e
nodata no resto. O render.py lista esse remendo ANTES do arquivo principal
(TIERS[…]["patches"]) e o mosaico do rio-tiler pega o 1º pixel válido: dentro
da lacuna vale o remendo, fora dela o tier de sempre.

Mesma receita do export do EE (tools/export_fabdem_tier.py):
  banda 1 elev  — MÉDIA de área da elevação nativa (m, int16)
  banda 2 slope — MÉDIA de área da declividade derivada NO GRID NATIVO (tan ×10000)
Declividade = compute_slope do render.py (diferença central, 111 320 m/° ×
cos(lat)) numa janela com 2 px de borda tirados das células vizinhas — sem
emenda na divisa das células. Pixel de 500 m = 15 px nativos; o grid nativo tem
meio pixel de deslocamento (centros em k/3600°), então a média pondera 16 px
com meio peso nas pontas (= média de área exata, como o reduceResolution).
Mar/célula ausente/nodata = 0 m (convenção do tier de 500 m).

Uso (lê ~1 GB dos COGs do R2; sem credencial nenhuma pra calcular):
    python tools/patch_tier_gap.py                  # gera ./fabdem_500m_gap_cop30.tif
    python tools/patch_tier_gap.py --upload         # + gcloud storage cp pro bucket
Depois de subir: `ready: True` no remendo em render.py (TIERS) + bump de
RENDER/TILE_VERSION e TERRAIN_VERSION (os tiles de zoom afastado ali estão em
cache de 7 dias como mar).
"""

import os
import subprocess
import sys
import time

import numpy as np
import rasterio
from rasterio.shutil import copy as rio_copy
from rasterio.windows import Window

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import render  # noqa: E402  (FABDEM_BASE_URL, fabdem_tile_name, FABDEM_CELLS, compute_slope)

NATIVE = 3600            # px por grau do FABDEM/COP30
PPD = 240                # px por grau do tier de 500 m
K = NATIVE // PPD        # 15 px nativos por pixel do tier
M = 2                    # borda (px nativos): 1 pra média nas pontas + 1 pra declividade
NODATA = -32768
OUT_NAME = "fabdem_500m_gap_cop30.tif"
DEST = f"gs://telhas/dem/fabdem_500m/{OUT_NAME}"


def gap_cells():
    """(lat_lo, lon_lo) da seção '# gap-cop30' do fabdem_cells.txt."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fabdem_cells.txt")
    out, on = [], False
    for line in open(p):
        c = line.strip()
        if c.startswith("# gap-cop30"):
            on = True
        elif on and c and not c.startswith("#"):
            out.append((int(c[1:3]) * (1 if c[0] == "N" else -1),
                        int(c[4:7]) * (1 if c[3] == "E" else -1)))
    return out


def read_window(lat_lo, lon_lo):
    """Elevação nativa da célula + M px de borda das vizinhas → (3600+2M+1)².
    Índice a ↔ px nativo i = a − M (linhas a partir do NORTE da célula; i = 3600
    é o 1º px da vizinha, que a média de área do último pixel do tier usa)."""
    n = NATIVE + 2 * M + 1
    a = np.zeros((n, n), np.float32)          # vizinha ausente = mar = 0 m
    for dy in (-1, 0, 1):                     # −1 = vizinha ao NORTE
        for dx in (-1, 0, 1):
            cell = (lat_lo - dy, lon_lo + dx)
            if cell not in render.FABDEM_CELLS:
                continue
            # intervalo de i (relativo à célula central) que cai nesta vizinha
            r0, r1 = max(-M, dy * NATIVE), min(NATIVE + M + 1, (dy + 1) * NATIVE)
            c0, c1 = max(-M, dx * NATIVE), min(NATIVE + M + 1, (dx + 1) * NATIVE)
            if r0 >= r1 or c0 >= c1:
                continue
            url = render.FABDEM_BASE_URL + render.fabdem_tile_name(*cell)
            with rasterio.open(url) as src:
                w = Window(c0 - dx * NATIVE, r0 - dy * NATIVE, c1 - c0, r1 - r0)
                blk = src.read(1, window=w).astype(np.float32)
                nd = src.nodata
            if nd is not None:
                blk[blk == nd] = 0.0              # nodata dentro de célula = mar
            a[r0 + M:r1 + M, c0 + M:c1 + M] = blk
    return a


def area_mean(x):
    """Média de área nos pixels do tier: pixel j cobre os px nativos i=15j..15j+15,
    com meio peso nas pontas (os centros nativos caem nas bordas). x: (3600+2M+1)²
    → (240, 240)."""
    def one_axis(v):
        v = v[M:M + NATIVE + 1]               # i = 0..3600
        cs = np.concatenate([np.zeros((1,) + v.shape[1:], v.dtype), np.cumsum(v, axis=0)])
        j = np.arange(PPD) * K
        s = cs[j + K + 1] - cs[j] - 0.5 * (v[j] + v[j + K])
        return s / K
    y = one_axis(x.astype(np.float64))
    return one_axis(y.T).T


def main(upload: bool):
    cells = gap_cells()
    if not cells:
        sys.exit("fabdem_cells.txt sem seção '# gap-cop30'")
    w = min(c[1] for c in cells); e = max(c[1] for c in cells) + 1
    s = min(c[0] for c in cells); n = max(c[0] for c in cells) + 1
    H, W = (n - s) * PPD, (e - w) * PPD
    elev = np.full((H, W), NODATA, np.int16)
    slope = np.full((H, W), NODATA, np.int16)
    rad = np.pi / 180.0
    for i, (lat_lo, lon_lo) in enumerate(cells, 1):
        t0 = time.time()
        a = read_window(lat_lo, lon_lo)
        # latitude de cada linha nativa (centros em lat_lo+1 − i/3600)
        lat = (lat_lo + 1) - (np.arange(a.shape[0]) - M) / NATIVE
        rx = (111320.0 / NATIVE) * np.cos(lat * rad)[:, None]
        ry = 111320.0 / NATIVE
        sl = render.compute_slope(a.astype(np.float64), np.ones(a.shape, bool), rx, ry)
        em, sm = area_mean(a), area_mean(sl)
        r0, c0 = (n - (lat_lo + 1)) * PPD, (lon_lo - w) * PPD
        elev[r0:r0 + PPD, c0:c0 + PPD] = np.round(em).astype(np.int16)
        slope[r0:r0 + PPD, c0:c0 + PPD] = np.minimum(np.round(sm * 10000), 32767).astype(np.int16)
        print(f"[{i}/{len(cells)}] {render.fabdem_tile_name(lat_lo, lon_lo)[:7]} "
              f"elev {int(em.min())}–{int(em.max())} m · slope p50 {np.median(sm):.3f} "
              f"({time.time() - t0:.0f} s)", flush=True)

    tmp = OUT_NAME + ".tmp.tif"
    prof = dict(driver="GTiff", width=W, height=H, count=2, dtype="int16", nodata=NODATA,
                crs="EPSG:4326", transform=rasterio.transform.from_origin(w, n, 1 / PPD, 1 / PPD),
                tiled=True, blockxsize=256, blockysize=256, compress="lzw")
    with rasterio.open(tmp, "w", **prof) as dst:
        dst.write(elev, 1); dst.write(slope, 2)
        dst.set_band_description(1, "elev"); dst.set_band_description(2, "slope")
        dst.update_tags(AREA_OR_POINT="Area")
    rio_copy(tmp, OUT_NAME, driver="COG", compress="LZW", blocksize=256,
             overview_resampling="AVERAGE")
    os.remove(tmp)
    print(f"→ {OUT_NAME}  bbox {w},{s},{e},{n}  {W}×{H}  {os.path.getsize(OUT_NAME) / 1e6:.1f} MB")
    if upload:
        subprocess.run(["gcloud", "storage", "cp", OUT_NAME, DEST], check=True)
        print("subiu:", DEST)
    else:
        print("subir:  gcloud storage cp", OUT_NAME, DEST)
    print("depois: ready=True no remendo (render.TIERS) + bump RENDER/TILE_VERSION e TERRAIN_VERSION")


if __name__ == "__main__":
    main("--upload" in sys.argv[1:])
