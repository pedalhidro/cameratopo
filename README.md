# Câmera Topográfica — tile server XYZ

Serve o relevo da **Câmera Topográfica** como tiles XYZ padrão
(`/{z}/{x}/{y}.png`), pra ser consumido como qualquer camada de tiles no
`web/app.js` (ou em qualquer cliente de mapa). Renderiza a MESMA imagem que o
app fazia no cliente — elevação na paleta cmocean.phase (cíclica, perceptual)
multiplicada por um realce de declividade branco→preto γ-corrigido — só que
por tile e no servidor, lendo os COGs do FABDEM / DEM de SP hospedados no
`telhas.pedalhidrografi.co` (read-only, via `/vsicurl/`).

Como o `eink/`, é **deliberadamente fora do backend do amora**: não toca
catálogo nem estado, só lê COGs públicos. Roda em qualquer lugar — laptop,
Raspberry Pi, ou um Cloud Run próprio atrás de `cameratopo.pedalhidrografi.co`.

## Por que os parâmetros são query string

Numa grade de tiles cada `z/x/y` é renderizado sozinho. Se a faixa de
normalização (elevação mín./máx., declividade máx.) fosse calculada por tile,
cada um normalizaria diferente e apareceriam **costuras** entre eles. Então
esses valores precisam ser **constantes em toda a grade** — vêm da querystring.
O valor especial `auto` é resolvido **uma vez** sobre uma região de referência
fixa (RMSP) e cacheado no processo, então continua uniforme e sem costura.

## Endpoint

```
GET /{z}/{x}/{y}.png
```

| Param        | Significado                                                        | Default |
|--------------|--------------------------------------------------------------------|---------|
| `elevMin`    | elevação (m) mapeada pro início da paleta. `auto` = p5 da RMSP      | `auto`  |
| `elevMax`    | elevação (m) mapeada pro fim da paleta. `auto` = p80 da RMSP        | `auto`  |
| `slopeMax`   | declividade (m/m) que satura em preto. `auto` = p98 da decliv. nativa| `auto` |
| `slopeGamma` | γ do realce de declividade (1 = linear; >1 suaviza)                 | `1.2`   |
| `cycles`     | quantas vezes a paleta se repete na faixa (contorno cíclico), 1–16  | `1`     |
| `dem`        | `fabdem` (global ~30 m) \| `sp` (DEM de São Paulo ~5 m)             | `fabdem`|
| `ss`         | teto da superamostragem no zoom afastado (px/lado), máx 1024        | `512`   |

`auto` também é assumido quando o parâmetro é omitido ou vem vazio. Fora da
cobertura do DEM o tile sai transparente.

```
GET /health   → {"ok": true}
```

## Reamostragem, zoom e custo

- **A declividade é SEMPRE derivada perto da resolução nativa do DEM** (~30 m
  FABDEM, ~5 m SP) e só então o campo é reescalado pro tile. É o mesmo modelo do
  app de Earth Engine (`setDefaultProjection(nativo)` + `ee.Terrain.slope` +
  pirâmide com reducer `mean`), e resolve os dois artefatos:
  - **Zoom-in** além do nativo: ler 256 px só interpolaria, e a declividade de
    uma superfície interpolada é constante por célula → **grade**. Lê ~1 px por
    célula nativa e reamplia (bilinear).
  - **Zoom-out**: um tile de z11 cobre ~600 células nativas; decimar a elevação
    pra 256 px ANTES de derivar a declividade serrilhava (moiré/degraus) e apagava
    a textura fina. Agora **superamostra** até `MAX_READ_SIZE`, lê com
    reamostragem por **área (`average`)** e reduz o campo por **média (BOX)**.
  - Knobs: `CAMERATOPO_MAX_READ_SIZE` (512, = 2× supersample),
    `CAMERATOPO_MIN_READ_SIZE` (8), `CAMERATOPO_FABDEM_NATIVE_M` (30),
    `CAMERATOPO_SP_NATIVE_M` (5).
- **`slopeMax` automático sai da declividade NATIVA.** A declividade depende da
  escala: tirar o p98 de um DEM decimado (a leitura de elevação do `/stats`, ~217
  m/px numa viewport de z11) subestimava ~2× → tudo saturava em preto e o ruído
  das áreas planas virava grade. O `/stats` amostra `CAMERATOPO_SLOPE_WINDOWS`²
  (2×2) janelas nativas de `CAMERATOPO_SLOPE_WIN_PX` (384) px, em paralelo.
- **Sem restrição de zoom** (`CAMERATOPO_MIN_ZOOM`=0, `MAX_ZOOM`=24). O custo é
  contido não por um piso de zoom, mas pela **guarda do mosaico FABDEM**: um tile
  cujo span passa de `CAMERATOPO_MOSAIC_MAX_SPAN` (6°) ou que precisaria de mais
  de `CAMERATOPO_MOSAIC_MAX_ASSETS` (40) COGs 1° sai transparente. O DEM-SP, COG
  único com overviews, serve qualquer zoom barato.

## UI navegável (`GET /`)

Abrir `cameratopo.pedalhidrografi.co/` (ou `http://127.0.0.1:8400/` local) dá uma
página estática — mapa Leaflet + painel de parâmetros — pra explorar o relevo e
achar uma combinação boa antes de embutir os tiles em outro cliente. Inspirada no
app de Earth Engine da Câmera Topográfica, mas self-contained e sem lock-in:
Leaflet é **vendorado** em `web/vendor/` (nada de CDN), só a busca de lugar
(Nominatim/OSM) e os tiles de base (OSM/Esri) batem em serviço externo.

- Navega o mapa, ajusta elevação mín./máx., declive máx., γ, ciclos e opacidade;
  cada mudança só reescreve a querystring dos tiles (`relief.setUrl`). O declive
  tem toggle **gradiente % ↔ graus** (só muda a exibição; internamente é sempre
  gradiente %).
- **Faixa automática (segue a tela)**: com auto ligado, a UI chama
  `GET /stats?bbox=…&dem=…` pela porção visível a cada navegação (debounced,
  valores arredondados p/ estabilizar o cache) e **congela números explícitos**
  na querystring — adaptável à tela E sem costura (mesmos números por toda a
  grade). Os valores aparecem esmaecidos nos campos. **Fixar valores desta
  vista** trava a estimativa atual em modo manual (editável).
- **Copiar URL de tiles (XYZ)** dá o template `…/{z}/{x}/{y}.png?…` pronto pra
  colar em qualquer cliente; **Copiar link desta vista** dá a página com todo o
  estado no hash da URL (`#map=z/lat/lng&…`), então uma configuração é
  compartilhável por link.

```
GET /stats?bbox=<oeste,sul,leste,norte>&dem=fabdem|sp
  → {"ok": true, "dem": "...", "elevMin": .., "elevMax": .., "slopeMax": ..}
```

`slopeMax` volta em **m/m** (a UI mostra em %). O bbox é limitado a 5° por lado
(defesa: sem teto, um bbox gigante enumeraria centenas de COGs FABDEM). Sem
cobertura de DEM → `{"ok": false}`. `GET /`, `/stats` e os assets de `web/` só
existem quando a página é servida — o serviço continua sendo, antes de tudo, um
tile server; um host estático-só (CDN) serve só a página, e o botão “Estimar”
fica sem backend.

Tiles são determinísticos por `(z, x, y, querystring)`: resposta com
`Cache-Control: public, max-age=7d` + `ETag` (com suporte a `If-None-Match` →
304) e `Access-Control-Allow-Origin: *`. Ponha Cloudflare/CDN na frente com a
querystring na chave de cache.

Sem auth — igual ao resto do projeto (restrinja na borda se precisar).

## Rodando local

```sh
pip install -r cameratopo/requirements.txt
python cameratopo/server.py          # http://127.0.0.1:8400
# ex.: http://127.0.0.1:8400/13/3035/4646.png?elevMin=720&elevMax=920&cycles=3
```

`python cameratopo/render.py` roda um smoke test offline (DEM sintético, sem
rede) que valida a matemática de declividade/paleta.

## Deploy (Cloud Run + Cloudflare)

`rasterio` traz wheels manylinux com GDAL embutido, então o `Dockerfile` parte
da `python:3.12-slim` (sem GDAL do sistema). Como o backend do amora, roda com
**1 worker** (o único estado é cache em memória; concorrência vem das threads,
escala por instâncias).

```sh
gcloud run deploy cameratopo \
  --source cameratopo \
  --region southamerica-east1 \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 4 --concurrency 40
```

Depois aponte `cameratopo.pedalhidrografi.co` pro serviço (domain mapping do
Cloud Run ou um worker/proxy da Cloudflare, como os outros subdomínios). Deixe
a Cloudflare cachear com a querystring na chave.

> Ajuste `--region`/nome ao seu projeto. O serviço só precisa de saída HTTPS
> pro host dos COGs (`telhas.pedalhidrografi.co`); não usa credencial nenhuma.
