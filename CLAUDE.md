# Câmera Topográfica (cameratopo)

Tile server XYZ de relevo + UI Leaflet, repo próprio da org (extraído do
`amora/` com história preservada; o amora só **consome** os tiles como camada).
Elevação (FABDEM global ~30 m / DEM-SP ~5 m, COGs em
`telhas.pedalhidrografi.co`) vira cor pela paleta cíclica **cmocean.phase**,
multiplicada por um realce de declividade branco→preto γ-corrigido. A matemática
é um porte servidor do app de Google Earth Engine — **`ee-cameratopo.js` é a
referência canônica do comportamento-alvo** (não roda aqui; documentação viva).

- `server.py` — Flask: `GET /{z}/{x}/{y}.png` (tiles), `GET /` + `/index.html`
  (UI), `GET /vendor/<p>`, `GET /stats` (percentis por bbox), `GET /health`.
- `render.py` — leitura de COG (rio-tiler//vsicurl) + declividade + paleta.
- `ee_source.py` — fonte `dem=ee`: a MESMA composição como expressão Earth
  Engine (getMapId + proxy dos PNGs; a referência canônica rodando de verdade).
  Auth por ADC — **nenhum segredo no repo**: no Cloud Run, deploy com
  `--service-account` de uma SA com `roles/earthengine.writer` (o getMapId
  precisa de `earthengine.maps.create`, que o viewer NÃO tem) +
  `serviceusage.serviceUsageConsumer` num projeto REGISTRADO no EE
  (`CAMERATOPO_EE_PROJECT`, default `pedal-hidrografico`); local, ADC do
  gcloud. mapids expiram ~4 h → cache TTL 3 h com double-checked lock, LRU 64
  chaves quantizadas (mesma precisão do ETag — jitter de slider não cunha
  mapid). `slopeMax` é m/m; `ee.Terrain.slope` dá GRAUS → `tan(rad(·))` antes
  de normalizar. `/stats?dem=ee` usa os percentis do fabdem LOCAL (mesmos
  dados; zero quota EE). Sem costura por construção (params moram no mapid).
  Falha de EE/rede → tile transparente, como as outras fontes. Versão própria
  (`EE_VERSION`) na chave/ETag — bumpe ao mudar a expressão EE (independente do
  `RENDER_VERSION`). Também abriga o **registry de camadas** do app GEE
  (`LAYERS`, endpoint `/ee/<id>/{z}/{x}/{y}.png`, painel ⧉ da UI): MapBiomas,
  luzes, aridez, WorldPop, eleições, claro3g, PTL, desmorro, declives,
  elevação, rios — cada uma uma expressão EE já visualizada, mapid cacheado
  por (camada, params que ela USA — `layer_param_sig`). Versão própria
  (`EE_LAYERS_VERSION`) — bumpe JUNTO com `EE_LAYER_VERSION` do index.html ao
  mudar qualquer expressão de camada. Paletas inline (gena/ee-palettes,
  MapBiomas col. 9 oficial) — o Code Editor resolve via require(), aqui é
  literal. Falha de camada NÃO entra em cache (nem servidor nem navegador,
  max-age=60): o 1º tile de camada pesada (worldpop/desmorro) estoura timeout
  enquanto o EE computa, e cachear congelaria o buraco. **Assets privados**
  (claro3g, eleições — `projects/ee-danilolessa/assets/*`): no Cloud Run a SA
  `cameratopo-ee@` precisa de LEITURA nos assets (compartilhar no Code Editor
  ou `earthengine acl ch -u serviceAccount:…:R <asset>`); sem acesso → tile
  transparente. Local (ADC do dono) sempre funciona.
- `osm_overlay.py` — camada **Traçado OSM** (`GET /osm/{z}/{x}/{y}.png`, linha
  "tracado" do painel ⧉): proxy do tile carto padrão do osm.org + extração POR
  COR (numpy, por pixel — sem vizinhança = sem costura por construção) que
  mantém só vias/ferrovias/água, recoloridas mais escuras/saturadas, resto
  alfa 0 — overlay estilo "touring" do OsmAnd pra pôr por cima do relevo.
  Cores-alvo calibradas contra tiles reais do carto; se o estilo do osm.org
  mudar, recalibrar e bumpar `OSM_OVERLAY_VERSION` JUNTO com
  `OSM_TRACADO_VERSION` do index.html. Distâncias em **int32** (int16
  estourava no quadrado e casava tudo). Falha de rede → transparente sem
  cache (max-age=60), como as camadas EE. User-Agent identificado + cache de
  7 dias (política de tiles do OSMF).
- `web/index.html` — a UI inteira (um só arquivo, sem build): Leaflet (+
  leaflet-rotate, GPL-3.0) e IBM Plex Mono **vendorados** em `web/vendor/`
  (nada de CDN), strings em PT, estado todo no hash da URL, crossfade de
  camadas de tile. É um **PWA**: `web/manifest.json` + `web/sw.js` (shell
  stale-while-revalidate; tiles/stats SÓ rede) + ícones renderizados pelo
  próprio render.py. **Bump do `VERSION` do sw.js em QUALQUER mudança de
  arquivo servido** (convenção do workspace) — além do par
  RENDER/TILE_VERSION quando pixels mudarem.
- Deploy: Cloud Run, projeto `pedal-hidrografico`, serviço `cameratopo`
  (`gcloud run deploy cameratopo --source . --region southamerica-east1
  --allow-unauthenticated --min-instances 0 --max-instances 4 --concurrency 40
  --service-account cameratopo-ee@pedal-hidrografico.iam.gserviceaccount.com`
  — a SA é o que dá ADC com acesso ao EE pra fonte `dem=ee`).
  Sem auth por design (igual ao resto do ecossistema).

## Invariantes do render — NÃO regredir (cada um já foi bug)

- **Tiles sem costura é um MUST.** Os parâmetros de normalização
  (elevMin/elevMax/slopeMax) têm que ser CONSTANTES em toda a grade num dado
  instante. Por isso: o `auto` do servidor resolve os percentis UMA vez sob
  `_auto_lock` (double-checked — resolver fora do lock deixava threads frias
  divergirem entre real e fallback = costura); a UI congela NÚMEROS explícitos
  vindos do `/stats` na querystring (adaptável à tela E uniforme).
- **Declividade é derivada ~na resolução NATIVA do DEM, sempre** (como o GEE:
  `setDefaultProjection(nativo)` + `ee.Terrain.slope` + pirâmide `mean`):
  - Zoom-in: ler 256 px só interpola, e a declividade de superfície interpolada
    é constante por célula → **grade**. Lê ~1 px/célula nativa e amplia.
  - Zoom-out: decimar a elevação ANTES de derivar declividade serrilha (moiré)
    e apaga textura. Superamostra até `ss` (≤ `SS_HARD_MAX`), lê por ÁREA
    (`average`) quando decima de verdade, reduz o campo por média (BOX).
  - A ampliação amostra DENTRO do array bufferizado por coordenada
    (`_bilinear_from_buffered`) — recortar o buffer antes de ampliar grampeia a
    borda e cada tile amplia isolado → degrau em toda emenda de 256 px.
- **`read_size` é POTÊNCIA DE 2** (`_pow2_floor`), nunca `round(native_px)`:
  `native_px` depende da latitude e o arredondamento oscilava entre linhas de
  tiles vizinhas (37/38 em z15) → grades de leitura diferentes → a declividade
  (derivada!) dava degrau na emenda. Potência de 2 = grade constante por zoom em
  faixas largas de latitude + reamostragens exatas (2^k divide 256).
- **`slopeMax` automático sai da declividade NATIVA** (`_slope_pct_native`,
  janelas nativas amostradas em paralelo): declividade depende da ESCALA — o
  p98 de um DEM decimado sai ~2× menor, satura o relevo em preto e transforma o
  ruído das áreas planas em grade. Elevação segue p5/p80 (o EE usa p2/p98, mas
  p5/p80 é o contraste escolhido).
- **`RENDER_VERSION` (render.py) e `TILE_VERSION` (web/index.html) andam
  JUNTOS** — bumpe os dois em qualquer mudança que altere pixels. Os tiles têm
  `Cache-Control` de 7 dias: o ETag sozinho NÃO fura o max-age (o navegador nem
  revalida), então sem o `v=` novo na URL o usuário continua vendo os PNGs
  antigos — inclusive "depois do fix".
- **Guardas de custo público**: mosaico FABDEM tem teto de span/nº de COGs por
  tile (`MOSAIC_MAX_*` → transparente); `/stats` tem `STATS_MAX_SPAN_DEG`;
  `ss` clampa em `SS_HARD_MAX`. Parse de query defensivo (`math.isfinite` —
  `cycles=1e999` já derrubou com OverflowError, que `except ValueError` NÃO
  pega).

## Gotchas de infra

- **Dockerfile precisa de `libexpat1`** na `python:3.12-slim`: sem ela o
  `import rasterio` quebra, o worker do gunicorn nunca sobe e o Cloud Run
  responde 503 em tudo — com o serviço parecendo `Ready` (o master do gunicorn
  passa no probe TCP). Foi a causa do serviço nunca ter servido um tile.
- **`gunicorn --workers 1`** (threads p/ concorrência) — convenção da casa.
- **O Worker da Cloudflare reescreve `/` → `/index.html`** (mesma convenção do
  amora) e o proxy TEM que apontar pro host `*.run.app` (Cloud Run dá 404 com
  Host customizado). Por isso `index()` está registrado nos DOIS paths.
- `.env`/segredos: não há — o serviço é read-only sobre COGs públicos.

## Verificar antes de terminar

- `python -m py_compile server.py render.py`; `python render.py` (smoke test
  offline da matemática).
- Mudou pixel? Bump `RENDER_VERSION` + `TILE_VERSION` (par).
- Mudou a UI? Carregue no navegador (JS inline: `node --check` não pega TDZ —
  um `let` lido antes da declaração já abortou o boot silenciosamente).
- Costura é regressão clássica: teste com um stitch 3×3+ medindo |Δ| na
  fronteira vs gradiente interior (~1.0× = sem costura), em zoom de AMPLIAÇÃO
  (z14/z15) e de REDUÇÃO (z11), nos dois DEMs.
