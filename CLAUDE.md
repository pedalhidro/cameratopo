# Câmera Topográfica (cameratopo)

Tile server XYZ de relevo + UI Leaflet, repo próprio da org (extraído do
`amora/` com história preservada; o amora só **consome** os tiles como camada).
Elevação (FABDEM global ~30 m, COGs no R2 em `fabdem.pedalhidrografi.co`, tiles
na raiz / DEM-SP ~5 m em `telhas.pedalhidrografi.co`) vira cor pela paleta
cíclica **cmocean.phase**,
multiplicada por um realce de declividade branco→preto γ-corrigido. A matemática
é um porte servidor do app de Google Earth Engine — **`ee-cameratopo.js` é a
referência canônica do comportamento-alvo** (não roda aqui; documentação viva).

- `server.py` — Flask: `GET /{z}/{x}/{y}.png` (tiles), `GET /` + `/index.html`
  (UI), `GET /vendor/<p>`, `GET /stats` (percentis por bbox), `GET /health`,
  `GET /terrain/{z}/{x}/{y}.png?dem=` (terreno do modo 3D, ver abaixo),
  `GET /field/{z}/{x}/{y}.png?dem=&ss=` (campos pro navegador colorir).
  Tile ainda na fila cujo cliente já desistiu (`_client_gone`, espia o socket
  do gunicorn) NÃO é renderizado → 499 no-store: tile cancelado (pan, camada
  trocada) enchia a fila de render inútil na frente dos tiles que importam.
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
- `web/index.html` — a UI inteira (um só arquivo, sem build): Leaflet (+
  leaflet-rotate, GPL-3.0) e IBM Plex Mono **vendorados** em `web/vendor/`
  (nada de CDN), strings em PT, estado todo no hash da URL, crossfade de
  camadas de tile. Camadas do painel ⧉ com campo `xyz` no catálogo são tiles
  XYZ diretos, sem EE: os MTPI
  Pindorama 90m/Bacia do Paraná 30m (telhas.pedalhidrografi.co, os mesmos do
  amora — nativos até z10/z12 via `zmax`). É um **PWA**: `web/manifest.json` + `web/sw.js` (shell
  stale-while-revalidate; tiles/stats SÓ rede) + ícones renderizados pelo
  próprio render.py. **Bump do `VERSION` do sw.js em QUALQUER mudança de
  arquivo servido** (convenção do workspace) — além do par
  RENDER/TILE_VERSION quando pixels mudarem.
- **Relevo colorido NO NAVEGADOR** (fontes fabdem/sp; `ee` segue PNG pronto):
  `/field/` entrega os CAMPOS (`render_fields` = tudo que era caro no
  `render_tile`, que agora é `render_fields` + `shade`) num PNG RGB OPACO
  256×512 — elevação Terrarium (1/16 m) em cima; declividade em LOG (2%,
  piso 1e-6) + máscara embaixo. Opaco de propósito: canvas pré-multiplica
  alfa. O `FieldLayer` (index.html) decodifica e pinta = `shade()` em JS
  (LUT de γ por código); mudar faixa/declive/γ/ciclos (e o auto a cada pan) só
  repinta — zero pedido. Paridade medida vs o PNG do servidor: ≤3 níveis.
  O encoding é CONTRATO: `FIELD_*` do render.py ↔ `FIELD_*` do index.html,
  bump de `FIELD_VERSION` nos dois. O PNG /{z}/{x}/{y}.png continua (amora,
  "Copiar URL telhas", camada do 3D).
- **Carregamento no cliente** (cada item já foi bug):
  - `LeanTileLayer`: depois de cada `_update` cancela tile AINDA CARREGANDO fora
    da vista — o `keepBuffer` do Leaflet poupa também o que nem chegou, e
    arrastar o mapa enfileirava dezenas de tiles renderizados à toa.
  - Tile de canvas precisa de `complete` (como `<img>`): o `_abortLoading` do
    Leaflet no zoom remove todo tile de outro zoom com `!complete` — sem isso o
    zoom jogava fora os tiles prontos e não reaproveitava pai/filhos.
  - Prévia (`ensurePreview`): até 2 zooms abaixo do zoom que o relevo PEDE
    (retina pede +1), nunca abaixo de `PREVIEW_MIN_Z` (hoje 0 pra todas as
    fontes: o tier de 500 m dá relevo FABDEM em qualquer zoom; se um tier sair
    de `ready`, suba o piso junto); recriada quando o deslocamento muda. PORTEIRA
    (`afterPreview`): os tiles cheios esperam a prévia do lote (ou 2,5 s).
- **Troca de fonte cancela a anterior** (`freezeRelief`): camada que sai de
  cena para de pedir tiles e aborta os pendentes; crossfade superado sai na
  hora. Antes cada troca de DEM enfileirava mais uma vista inteira
  (144 → 288 → 432). Relevo/⧉ com `updateWhenIdle: true` (sem tile varrido no
  arrasto).
- **Modo 3D** (botão 3D): MapLibre GL 5.24 **vendorado** em
  `web/vendor/maplibre-gl/` e carregado SÓ quando liga. O Leaflet segue como
  mapa mestre (hash, /stats, busca, 📍) e no 3D fica SEM camadas de tile
  (`in3d` no `syncEeLayers` — senão o servidor renderiza tudo duas vezes);
  acompanha a câmera do MapLibre no moveend. z3d = z2d − 1 (mundo de 512 vs
  256 px); bearing3d = −bearing2d (leaflet-rotate gira horário). Terreno =
  `/terrain/` em **Terrarium** do DEM selecionado (`ee` → FABDEM; `sp`
  completa fora da cobertura com FABDEM, senão a borda vira penhasco). Tile de
  terreno **NUNCA transparente**: o MapLibre lê (0,0,0) como −32768 m. Nada
  lido (oceano OU falha de R2 — indistinguíveis) → plano a 0 m com max-age=60
  e sem cache. `TERRAIN_VERSION` (render.py) e `TERRAIN_VERSION` do
  index.html andam JUNTOS; `TERRAIN_MAXZOOM` idem. Controles: exagero
  vertical e campo de visão (`setVerticalFieldOfView`), ambos no hash; pad
  de câmera (segurar = rAF com taxa/s: mover relativo ao bearing; girar e
  inclinar em PRIMEIRA PESSOA — `lookAround` mantém a câmera no mesmo x-y-z,
  centro recolocado na nova linha de visada e corrigido pelo erro medido
  contra uma âncora do toque (`pinCamera`), sem deriva; o arrasto do mouse
  segue orbitando; FOV age como LENTE — `applyFov` anota a câmera, muda o FOV
  e `pinCamera` a devolve (o MapLibre recuava/avançava a câmera);
  teclado W/S A/D Q/Z E/C 1/3 por `e.code`, ações combináveis num Set;
  "Velocidade" (`P.spd`, ×0,1–×10, hash `spd`) escala o dt do pad/teclado;
  modo mouse/FPS (M): Pointer Lock no canvas, deltas acumulados e aplicados
  1×/quadro via lookAround (observador parado), Leaflet só sincroniza na
  saída; "no chão" (G): o hook fixa a câmera em chão sob ela + `walkH` (Q/Z
  mudam walkH), soltar recalcula o centerLift pra ficar onde está. O CHÃO SOB
  A CÂMERA vem de `groundAt` (nosso /terrain/ amostrado, cache, × exagero):
  o terreno do MapLibre só tem tiles À VISTA e devolve 0 fora deles — olhando
  pro horizonte, o chão sob o observador saía 0 e o "no chão" afundava pro mar; altitude move a câmera na VERTICAL deslocando a altura do centro
  — `centerLift` — zoom aproximava do centro). Inclinação 0–90°: centro LIVRE
  (`centerClampedToGround: false`) + `transformCameraUpdate:
  keepCameraAboveGround` (altura do centro = máx(chão do centro + centerLift,
  o que deixa a câmera ≥ 30 m acima do chão sob ela)); o hook só roda em
  gesto/jumpTo, então `settleCamera()` no load e a cada idle (só se mudar —
  jumpTo vazio faria loop move→idle). Sem isso, em 90° a câmera ficava na
  altura do chão do alvo, dentro do relevo exagerado. FOV mexe no LOD dos tiles: `applyTileLod` ajusta
  `setSourceTileLodParams` pra penalidade de tile inclinado ficar = 1 em
  qualquer FOV (senão 5° pedia tiles 1–2 zooms abaixo = borrado). Estilo com `transition: {duration: 0}` + 
  `freeRtt()` após mudar paint: com terreno as camadas viram textura cacheada
  (RTT) capturada no 1º quadro da transição — a opacidade ficava um passo
  atrasada.
- Deploy: Cloud Run, projeto `pedal-hidrografico`, serviço `cameratopo`
  (`gcloud run deploy cameratopo --source . --region southamerica-east1
  --allow-unauthenticated --cpu 2 --memory 1Gi --min-instances 0 --max-instances 10 --concurrency 40
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
- **A reamostragem tem que chegar ao WARP**: os dois DEMs são EPSG:4326, e o
  rio-tiler lê por um WarpedVRT já na resolução de saída — quem reamostra é o
  `reproject_method` (default `nearest`!), não o `resampling_method`. Passar
  só este último fazia o nearest pular 1 coluna a cada ~4 px e 1 linha a cada
  ~7 (633→512 em z11) → GRADE fina na declividade de todo tile. `read_dem_tile`
  passa o mesmo método aos dois.
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
- **Tiers de resolução reduzida** (`TIERS` no render.py, cada um com `ready`;
  o de 500 m tem o MAR gravado como 0 — não nodata —, e o /stats o exclui; gerados por
  `tools/export_fabdem_tier.py` no EE → `gs://telhas/dem/<nome>/`, lidos (como o DEM-SP)
  direto de storage.googleapis.com — mesma região do Cloud Run = transferência
  grátis; o EE não exporta pro R2): 500 m
  global (4×2 arquivos de 90°) e 90 m América do Sul (arquivos de 10°). Banda 1
  = média da elevação, banda 2 = média da declividade NATIVA (tan ×10000) —
  derivada no grid de 30 m e só depois agregada (invariante acima); o render
  NÃO deriva nada do tier (sem buffer, média de área = sem costura). Por tile:
  o tier mais GROSSO, `ready`, com ≥ 256 px (px de SAÍDA, não `ss` — não há
  declividade a derivar) que CONTÉM o tile inteiro, senão o mosaico nativo:
  z ≤ 8 → 500 m; z9–10 na AS → 90 m. O tier dá a declividade NATIVA média
  (medido: p50 ~3× a do mosaico em z8, que lê overview de 60 m) — coerente
  com z11+ e com o EE. Arquivos do EE: `<nome><linha>-<coluna>.tif` (SEM hífen
  depois do nome). Terreno 3D idem. Nomes dos arquivos = offsets em px do EE a partir da origem NO
  do tier. Mudou o tier → bumpe RENDER/TILE_VERSION e TERRAIN_VERSION.
- **Mar = 0 m, falha ≠ mar** (FABDEM e EE): célula 1°×1° fora de
  `fabdem_cells.txt` (lista da coleção do EE; está no COPY do Dockerfile) é mar
  → 0 m sem pedido; nodata dentro de célula/tier também. COG que EXISTE e não
  leu → `DEMReadError` → resposta SEM cache (field 503, PNG transparente
  max-age 60). Nunca cachear falha como vazio/mar (ficaria 7 dias).
  `/stats` continua ignorando o mar (percentis só de terra).
- **`/stats` em zoom afastado usa o TIER**: viewport ≥ 1,5° (z ≤ 10) tira os
  percentis do tier que desenha esses zooms (`stats_tier_for` → `_tier_stats`:
  banda 1 p5/p80, banda 2 = declividade nativa média p98; mar fora), recortada
  à extensão do tier que cobre ≥ 50% da vista; aí o teto do endpoint sobe de
  5° pra 360° (o globo em ~0,5 s pelos menores overviews do tier de 500 m). Sem tier cobrindo, segue o caminho nativo (≤ 5°). Antes o `/stats`
  recusava viewport > 5° e o "auto" não funcionava em z ≤ 8.
- **Guardas de custo público**: mosaico FABDEM tem teto de span/nº de COGs por
  tile (`MOSAIC_MAX_*` → transparente; 6° e 49 COGs = z6 inteiro) — na prática
  só pesa com tier fora de `ready` ou `ss` alto, porque z ≤ 8 sai do tier;
  `/stats` tem `STATS_MAX_SPAN_DEG` (5°, 360° quando há tier cobrindo);
  `ss` clampa em `SS_HARD_MAX`. Parse de query defensivo (`math.isfinite` —
  `cycles=1e999` já derrubou com OverflowError, que `except ValueError` NÃO
  pega).

## Gotchas de infra

- **Módulo .py novo TEM que entrar no `COPY` do Dockerfile** (a lista é
  explícita): sem ele o import quebra SÓ no container → worker do gunicorn
  não sobe → 503 em tudo com o serviço `Ready` (mesmo sintoma do libexpat1;
  já aconteceu com um módulo de overlay, depois removido).
- **Dockerfile precisa de `libexpat1`** na `python:3.12-slim`: sem ela o
  `import rasterio` quebra, o worker do gunicorn nunca sobe e o Cloud Run
  responde 503 em tudo — com o serviço parecendo `Ready` (o master do gunicorn
  passa no probe TCP). Foi a causa do serviço nunca ter servido um tile.
- **Estimador de custo da UI** (pílula "custo desta sessão ≈ R$"): o servidor
  carimba tile/API com `Server-Timing: app;dur, at;desc` (after_request) e o
  navegador precifica cada PerformanceResourceTiming ×3 (segurança), em R$
  pela PTAX de venda do BCB (`/fx`, cache 6 h). `COST.vcpu/memGib` do
  index.html = flags `--cpu/--memory` do deploy — mudar JUNTO; preços de
  tabela Tier 2 (southamerica-east1) do Cloud Billing Catalog (conferidos
  contra a fatura). Leituras de DEM: o servidor põe `src;desc="r2|gcs"` no
  Server-Timing quando RENDERIZA (hit de cache não manda) — `_storage_src`,
  mesma regra de tier do render — e o navegador precifica ~4 leituras pelo
  preço de cada armazenamento. Acumulado por
  navegador em localStorage (`cameratopo-cost-v1`, USD cru por dia). Serviço
  inteiro = FATURA real via `/costs` (BigQuery `billing_export`, recurso Cloud
  Run `cameratopo`, cache 6 h) — a SA precisa de `roles/bigquery.jobUser` no
  projeto + leitura (READER) no dataset `billing_export`; sem isso a UI mostra
  "indisponível".
- **Capacidade: `--max-instances 10`.** Com 4 × concurrency 40 = 160 pedidos
  em voo, uma única vista retina em z7 (~144 tiles lentos de mosaico) batia o
  teto e o Cloud Run respondia **429** (10–21% dos tiles nos logs). Sem
  instância mínima (custo): a UI avisa o cold start (`checkServerAwake`).
- **Memória: 1 GiB.** Com 512 MiB o serviço estourou (OOM, instância morta no
  meio dos renders → tile de 90 s + cold start) assim que o PNG parou de
  segurar o GIL (mais renders simultâneos) e o cache LRU guardava campos de
  ~170 KB. O cache agora tem teto em BYTES (`CAMERATOPO_CACHE_MB`, 64) e o
  `GDAL_CACHEMAX` é explícito no Dockerfile.
- **`gunicorn --workers 1`** (threads p/ concorrência) — convenção da casa.
  Com o PNG em `compress_level=1` (o `optimize=True` custava ~65 ms/tile e
  segurava o GIL) 1 worker escala bem até 4 vCPU (medido: 24 tiles z10 em
  1/2/4 núcleos ≈ 5,5/3,5/2,6 s; 4 workers ≈ 2,3 s) — depois disso o piso é a
  leitura do R2.
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
