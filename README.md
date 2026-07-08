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
| `slopeMax`   | declividade (m/m) que satura em preto. `auto` = p80 da declividade  | `auto`  |
| `slopeGamma` | γ do realce de declividade (1 = linear; >1 suaviza)                 | `1.2`   |
| `cycles`     | quantas vezes a paleta se repete na faixa (contorno cíclico), 1–16  | `1`     |
| `dem`        | `fabdem` (global ~30 m) \| `sp` (DEM de São Paulo ~5 m)             | `fabdem`|

`auto` também é assumido quando o parâmetro é omitido ou vem vazio. Fora da
cobertura do DEM (ou fora da faixa de zoom) o tile sai transparente.

```
GET /health   → {"ok": true}
```

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
