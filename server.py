"""Servidor XYZ da Câmera Topográfica (projeto paralelo, tipo o eink/).

Serve tiles PNG do relevo em `GET /{z}/{x}/{y}.png` lendo os COGs do FABDEM /
DEM de SP (read-only, /vsicurl/). DELIBERADAMENTE fora do backend do amora —
não toca catálogo/estado; roda em qualquer lugar (laptop, Cloud Run próprio).

    pip install -r requirements.txt
    python server.py            # http://127.0.0.1:8400

Endpoint:
    GET /{z}/{x}/{y}.png   tile do relevo. Query params (todos opcionais):
        elevMin, elevMax   faixa de elevação da paleta, em metros. `auto` (ou
                           omitido) = percentil p5 / p80 da região de referência.
        slopeMax           declividade (m/m) que satura em preto. `auto` = p98.
        slopeGamma         γ do realce de declividade (default 1.2).
        cycles             quantas vezes a paleta se repete na faixa (default 1).
        dem                fabdem (default) | sp (DEM de SP ~5 m) | ee (FABDEM
                           renderizado pelo Google Earth Engine — proxy; ver
                           ee_source.py; requer ADC com acesso ao EE).
        ss                 teto da superamostragem no zoom afastado, em px por
                           lado (default 512, máx 1024). Mais = declividade mais
                           perto do nativo (mais textura), mais CPU/rede.
    GET /health            ok

Sem auth (igual ao resto do projeto — restrinja na borda se precisar). Tiles são
determinísticos por (z,x,y,querystring) → Cache-Control longo + ETag; ponha um
CDN/Cloudflare na frente com a query na chave de cache.
"""

import hashlib
import json
import math
import os
import socket
import threading
import time
import urllib.request

from flask import Flask, Response, g, jsonify, request, send_from_directory

import ee_source
import osm_overlay
import render

app = Flask(__name__)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# Faixa de zoom só como sanidade (z válido + x/y dentro de 2^z). NÃO é mais o
# limite prático: quem contém o custo é a guarda do mosaico FABDEM em render.py
# (span/contagem de COGs) — o DEM-SP, sendo COG único com overviews, serve
# qualquer zoom barato. Defaults abertos (0–24) = "sem restrição de zoom".
MIN_ZOOM = int(os.environ.get("CAMERATOPO_MIN_ZOOM") or 0)
MAX_ZOOM = int(os.environ.get("CAMERATOPO_MAX_ZOOM") or 24)
CACHE_MAX_AGE = int(os.environ.get("CAMERATOPO_MAX_AGE") or 604800)  # 7 dias

# Maior span (graus) aceito no /stats — a UI só manda a viewport (pequena), mas
# o endpoint é público: sem teto, um bbox gigante enumeraria centenas de COGs
# FABDEM 1°×1°. 5° cobre qualquer viewport plausível com folga.
STATS_MAX_SPAN_DEG = float(os.environ.get("CAMERATOPO_STATS_MAX_SPAN") or 5.0)

# GDAL/vsicurl: leitura eficiente de COG remoto (só ranges, sem listar diretório).
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "YES")
os.environ.setdefault("VSI_CACHE", "TRUE")


# ── Estimador de custo da UI ────────────────────────────────────────────────
# Toda resposta de tile/API leva `Server-Timing: app;dur=<ms>, at;desc=<epoch ms>`:
# a PARCELA deste pedido no tempo faturável da instância e QUANDO foi gerado. O
# navegador soma (PerformanceResourceTiming.serverTiming) e precifica. `at`
# denuncia resposta servida de cache de borda (a Cloudflare guarda o header
# junto): gerada muito antes do pedido → não custou Cloud Run.
#
# Parcela, não duração: o Cloud Run (cobrança por requisição) fatura o tempo em
# que a instância tem ≥1 pedido em voo — com 8 threads, somar as durações
# contaria o mesmo segundo até 8×. Um "relógio virtual" que anda 1/n com n
# pedidos em voo dá a cada pedido a sua fatia; a soma das fatias = tempo
# faturado exato (fora cold start e o arredondamento de 100 ms).
_TIMED = ("/field/", "/terrain/", "/ee/", "/osm/", "/stats", "/fx", "/health")
_busy = {"n": 0, "v": 0.0, "t": time.monotonic()}
_busy_lock = threading.Lock()


def _busy_advance(now):
    if _busy["n"] > 0:
        _busy["v"] += (now - _busy["t"]) / _busy["n"]
    _busy["t"] = now


@app.before_request
def _t0():
    g.t0 = time.perf_counter()
    with _busy_lock:
        _busy_advance(time.monotonic())
        _busy["n"] += 1
        g.v0 = _busy["v"]
    g.busy_open = True


def _busy_close():
    """Fecha a conta do pedido (uma vez) e devolve a parcela em segundos."""
    if not getattr(g, "busy_open", False):
        return 0.0
    g.busy_open = False
    with _busy_lock:
        _busy_advance(time.monotonic())
        _busy["n"] -= 1
        return _busy["v"] - g.v0


@app.after_request
def _server_timing(resp):
    share = _busy_close()
    p = request.path
    if p.endswith(".png") or p.startswith(_TIMED):
        wall = (time.perf_counter() - getattr(g, "t0", time.perf_counter())) * 1000.0
        resp.headers["Server-Timing"] = (f'app;dur={share * 1000.0:.1f}, wall;dur={wall:.1f}, '
                                         f'at;desc="{int(time.time() * 1000)}"')
    return resp


@app.teardown_request
def _busy_teardown(_exc):
    _busy_close()   # exceção sem after_request: não deixa o contador preso


# Câmbio USD→BRL: PTAX de venda do Banco Central (fonte oficial), buscada no
# SERVIDOR (a API do BCB não manda CORS) e cacheada FX_TTL_S. Falha → última
# boa ou FX_FALLBACK (marcado como tal, a UI mostra).
FX_TTL_S = 6 * 3600
FX_FALLBACK = float(os.environ.get("CAMERATOPO_FX_FALLBACK") or 5.18)
_fx = {"usdbrl": None, "date": None, "t": 0.0}
_fx_lock = threading.Lock()


def _fetch_ptax():
    end = time.strftime("%m-%d-%Y", time.gmtime())
    ini = time.strftime("%m-%d-%Y", time.gmtime(time.time() - 10 * 86400))  # cobre feriados/fds
    u = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
         "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
         f"?@dataInicial='{ini}'&@dataFinalCotacao='{end}'"
         "&$format=json&$orderby=dataHoraCotacao%20desc&$top=1")
    req = urllib.request.Request(u, headers={"User-Agent": "cameratopo/fx"})
    with urllib.request.urlopen(req, timeout=8) as r:
        v = json.load(r)["value"][0]
    return float(v["cotacaoVenda"]), v["dataHoraCotacao"][:10]


@app.get("/fx")
def fx():
    """USD→BRL (PTAX venda, BCB) pro estimador de custo da UI."""
    with _fx_lock:
        if _fx["usdbrl"] is None or time.time() - _fx["t"] > FX_TTL_S:
            try:
                _fx["usdbrl"], _fx["date"] = _fetch_ptax()
                _fx["t"] = time.time()
            except Exception as exc:  # noqa: BLE001
                app.logger.warning("PTAX falhou: %s", exc)
                _fx["t"] = time.time() - FX_TTL_S + 600   # tenta de novo em 10 min
        if _fx["usdbrl"] is None:
            return _json_cors({"usdbrl": FX_FALLBACK, "date": None, "source": "fallback"}, 200)
        return _json_cors({"usdbrl": _fx["usdbrl"], "date": _fx["date"], "source": "PTAX/BCB"}, 200)


def _fnum(name):
    """Float finito da query, ou None se ausente/inválido/`auto`."""
    v = request.args.get(name)
    if v is None:
        return None
    v = v.strip()
    if v == "" or v.lower() == "auto":
        return None
    try:
        f = float(v.replace(",", "."))
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _dem_arg():
    """Fonte da query: fabdem (default) | sp | ee."""
    v = (request.args.get("dem") or "").lower()
    return v if v in ("sp", "ee") else "fabdem"


def _stats_dem(dem):
    """A fonte `ee` é o MESMO FABDEM dos COGs — percentis vêm do caminho local
    (sem reduceRegion no EE: zero latência extra, zero quota)."""
    return "fabdem" if dem == "ee" else dem


def _resolve_params(dem):
    """Resolve os params da query, substituindo `auto`/ausente pelos percentis
    cacheados da região de referência. Retorna dict pronto pro render."""
    elev_min = _fnum("elevMin")
    elev_max = _fnum("elevMax")
    slope_max = _fnum("slopeMax")

    if elev_min is None or elev_max is None or slope_max is None:
        st = render.auto_stats(_stats_dem(dem))
        if elev_min is None:
            elev_min = st["elevMin"]
        if elev_max is None:
            elev_max = st["elevMax"]
        if slope_max is None:
            slope_max = st["slopeMax"]

    if elev_max <= elev_min:              # faixa inválida → degenera com graça
        elev_max = elev_min + 1.0

    gamma = _fnum("slopeGamma") or 1.2
    gamma = min(16.0, max(0.0625, gamma))   # UI usa escala log2: 1/16 … 16
    slope_max = max(1e-9, slope_max)

    cyc = request.args.get("cycles")
    try:
        f = float(cyc) if cyc else 1.0
        cycles = int(f) if math.isfinite(f) else 1  # inf → int() estouraria (OverflowError)
    except (TypeError, ValueError):
        cycles = 1
    cycles = min(16, max(1, cycles))

    # `ss`: teto da superamostragem (px lidos por lado no zoom afastado). Mais =
    # declividade mais perto do nativo, mais caro. Clampado em render.SS_HARD_MAX.
    ss = _fnum("ss")
    max_read = int(ss) if ss else render.MAX_READ_SIZE
    max_read = max(render.MIN_READ_SIZE, min(render.SS_HARD_MAX, max_read))

    # PTL (camada /ee/ptl/): ±N desvios que saturam a paleta e diâmetro (m) do
    # círculo de vizinhança. Clampes largos vs. o app GEE (0.1–3 / 60–1000) mas
    # com teto — kernel gigante = custo de computação no EE (endpoint público).
    ptl_sd = _fnum("ptlSd") or ee_source.PTL_MAX_SD
    ptl_sd = min(10.0, max(0.1, ptl_sd))
    ptl_kernel = _fnum("ptlKernel") or ee_source.PTL_KERNEL_M
    ptl_kernel = min(3000.0, max(30.0, ptl_kernel))

    return dict(elev_min=elev_min, elev_max=elev_max, slope_max=slope_max,
                gamma=gamma, cycles=cycles, max_read=max_read,
                ptl_sd=ptl_sd, ptl_kernel=ptl_kernel)


def _client_gone():
    """O cliente já desistiu deste pedido? (best-effort, só sob gunicorn)

    O navegador cancela tile que saiu da tela / de camada trocada, mas o pedido
    já pode estar na FILA do gunicorn (8 threads, concurrency 40): sem isto o
    servidor renderizava depois tile que ninguém mais ia ver — e a fila de
    lixo atrasava os tiles que importam. Espia o socket sem consumir: EOF =
    a outra ponta fechou. Pipelining/keep-alive podem mostrar bytes do PRÓXIMO
    pedido → "vivo" (erra pro lado seguro, só renderiza)."""
    sock = request.environ.get("gunicorn.socket")
    if sock is None:
        return False
    try:
        return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False            # nada a ler = conexão aberta, esperando a resposta
    except OSError:
        return True


# Resposta pra quem já foi embora: ninguém lê, mas o gunicorn precisa de uma.
# no-store — jamais pode virar cache de tile vazio.
def _gone_response():
    return Response(status=499, headers={"Cache-Control": "no-store"})


def _png_response(body, etag, max_age=None):
    inm = request.headers.get("If-None-Match")
    headers = {
        "Content-Type": "image/png",
        "Cache-Control": f"public, max-age={CACHE_MAX_AGE if max_age is None else max_age}",
        "ETag": etag,
        "Access-Control-Allow-Origin": "*",   # tiles públicos, consumidos por vários hosts
    }
    if inm and inm == etag:
        return Response(status=304, headers=headers)
    return Response(body, headers=headers)


@app.get("/health")
def health():
    return jsonify(ok=True)


# ── UI navegável (opcional; o serviço continua sendo antes de tudo um tile
#    server). Serve a página estática de web/ e seus assets vendorados. ────────
@app.get("/")
@app.get("/index.html")   # o worker da Cloudflare reescreve / → /index.html (como no amora)
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/vendor/<path:p>")
def vendor(p):
    # Só o dir vendorado (Leaflet); send_from_directory já barra path traversal.
    return send_from_directory(os.path.join(WEB_DIR, "vendor"), p)


# PWA + SEO: whitelist explícita (nada de servir web/ inteiro por rota genérica)
@app.get("/<any('manifest.json', 'sw.js', 'icon-192.png', 'icon-512.png',"
         " 'icon-512-maskable.png', 'robots.txt', 'sitemap.xml', 'llms.txt',"
         " 'og.png'):f>")
def pwa_file(f):
    resp = send_from_directory(WEB_DIR, f)
    if f == "sw.js":
        # O navegador respeita max-age no script do SW — curto, pra um deploy
        # com VERSION nova ser visto logo (o shell troca no ciclo do SW).
        resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.get("/favicon.ico")
def favicon():
    path = os.path.join(WEB_DIR, "favicon.ico")
    if os.path.exists(path):
        return send_from_directory(WEB_DIR, "favicon.ico")
    return Response(status=204)


@app.get("/stats")
def stats():
    """Percentis (elevMin p5, elevMax p80, slopeMax p98) sobre a extensão atual
    do mapa — alimenta o botão "Fixar valores desta vista" e o modo "auto segue a
    tela" da UI. A UI congela
    esses números explícitos na querystring dos tiles, então continua uniforme
    (sem costura). Query: bbox=oeste,sul,leste,norte (graus) & dem=fabdem|sp|ee
    (`ee` usa os percentis do fabdem local — mesmos dados)."""
    dem = _stats_dem(_dem_arg())
    raw = request.args.get("bbox") or ""
    try:
        w, s, e, n = (float(v) for v in raw.split(","))
    except (ValueError, TypeError):
        return _json_cors({"ok": False, "error": "bbox inválido"}, 400)
    if not all(math.isfinite(v) for v in (w, s, e, n)):
        return _json_cors({"ok": False, "error": "bbox inválido"}, 400)
    # Normaliza e valida a ordenação/tamanho (defende de bbox degenerado/gigante).
    w, e = min(w, e), max(w, e)
    s, n = min(s, n), max(s, n)
    w = max(-180.0, w); e = min(180.0, e)
    s = max(-85.06, s); n = min(85.06, n)
    if e <= w or n <= s:
        return _json_cors({"ok": False, "error": "bbox degenerado"}, 400)
    if (e - w) > STATS_MAX_SPAN_DEG or (n - s) > STATS_MAX_SPAN_DEG:
        return _json_cors({"ok": False, "error": "bbox grande demais"}, 400)

    try:
        st = render.stats_for_bbox(dem, (w, s, e, n))
    except Exception as exc:  # noqa: BLE001 — nunca derruba o endpoint
        app.logger.warning("stats %s falhou: %s", raw, exc)
        st = None
    if st is None:
        return _json_cors({"ok": False, "error": "sem cobertura de DEM aqui"}, 200)
    return _json_cors({"ok": True, "dem": dem, **st}, 200)


def _json_cors(obj, status):
    resp = jsonify(obj)
    resp.status_code = status
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.get("/<int:z>/<int:x>/<int:y>.png")
def tile(z, x, y):
    dem = _dem_arg()

    # Chave de cache/ETag: z/x/y + params já resolvidos (querystring canônica).
    p = _resolve_params(dem)
    # `rv` (versão do renderizador) na chave/ETag: senão, mudar a matemática do
    # render mantém o mesmo ETag → 304 → navegador/CDN servem o PNG antigo.
    # A fonte `ee` tem versão própria (EE_VERSION): `ss` não se aplica a ela e
    # a expressão EE evolui independente do render local.
    if dem == "ee":
        key = (f"ee{ee_source.EE_VERSION}/{z}/{x}/{y}"
               f"?e={p['elev_min']:.3f},{p['elev_max']:.3f}"
               f"&s={p['slope_max']:.6f}&g={p['gamma']:.3f}&c={p['cycles']}")
    else:
        key = (f"rv{render.RENDER_VERSION}/{dem}/{z}/{x}/{y}"
               f"?e={p['elev_min']:.3f},{p['elev_max']:.3f}"
               f"&s={p['slope_max']:.6f}&g={p['gamma']:.3f}&c={p['cycles']}"
               f"&ss={p['max_read']}")
    etag = '"' + hashlib.md5(key.encode()).hexdigest() + '"'

    if not (MIN_ZOOM <= z <= MAX_ZOOM):
        return _png_response(render.transparent_png(), etag)

    max_tile = 2 ** z - 1
    if not (0 <= x <= max_tile and 0 <= y <= max_tile):
        return _png_response(render.transparent_png(), etag)

    body = render.cache_get(key)
    if body is None:
        if _client_gone():
            return _gone_response()
        try:
            if dem == "ee":
                body = ee_source.fetch_tile(z, x, y, p)
            else:
                body = render.render_tile(
                    dem, x, y, z,
                    elev_min=p["elev_min"], elev_max=p["elev_max"],
                    slope_max=p["slope_max"], gamma=p["gamma"], cycles=p["cycles"],
                    max_read=p["max_read"],
                )
        except Exception as exc:  # noqa: BLE001 — nunca derruba o tile server
            app.logger.warning("render %s falhou: %s", key, exc)
            body = None
        if body is None:
            body = render.transparent_png()
        render.cache_put(key, body)

    return _png_response(body, etag)


@app.get("/ee/<layer>/<int:z>/<int:x>/<int:y>.png")
def ee_layer_tile(layer, z, x, y):
    """Tiles das camadas do app GEE (registry em ee_source.LAYERS) — mesma
    mecânica do dem=ee: mapid cacheado + proxy + transparente em falha. A UI
    (painel de camadas ⧉) monta a URL só com os params que a camada usa."""
    if layer not in ee_source.LAYERS:
        return Response(status=404)

    p = _resolve_params("ee")
    sig = ee_source.layer_param_sig(layer, p)
    key = f"eely{ee_source.EE_LAYERS_VERSION}/{layer}/{z}/{x}/{y}?{sig}"
    etag = '"' + hashlib.md5(key.encode()).hexdigest() + '"'

    max_tile = 2 ** z - 1
    if not (MIN_ZOOM <= z <= MAX_ZOOM and 0 <= x <= max_tile and 0 <= y <= max_tile):
        return _png_response(render.transparent_png(), etag)

    body = render.cache_get(key)
    if body is None:
        if _client_gone():
            return _gone_response()
        try:
            body = ee_source.fetch_layer_tile(layer, z, x, y, p)
            render.cache_put(key, body)
        except Exception as exc:  # noqa: BLE001 — nunca derruba o tile server
            app.logger.warning("camada ee %s falhou: %s", key, exc)
            # Falha NÃO entra no cache (nem servidor, nem 7 dias no navegador):
            # o 1º tile de camada pesada (worldpop, desmorro) costuma estourar
            # timeout enquanto o EE computa — cachear o transparente congelaria
            # o buraco mesmo depois do EE aquecer. max-age curto → retry.
            return _png_response(render.transparent_png(), etag, max_age=60)

    return _png_response(body, etag)


@app.get("/field/<int:z>/<int:x>/<int:y>.png")
def field_tile(z, x, y):
    """Campos (elevação + declividade + máscara) do tile, pro NAVEGADOR colorir
    (render.field_tile). Independe de elevMin/Max, slopeMax, γ e ciclos → mudar
    params não refaz nada no servidor, e o mesmo tile serve todo mundo (cache
    longo + CDN). `ee` não tem campos (o EE entrega RGB pronto) → fabdem."""
    dem = "sp" if _dem_arg() == "sp" else "fabdem"
    ss = _fnum("ss")
    max_read = int(ss) if ss else render.MAX_READ_SIZE
    max_read = max(render.MIN_READ_SIZE, min(render.SS_HARD_MAX, max_read))
    key = (f"fld{render.FIELD_VERSION}.rv{render.RENDER_VERSION}/{dem}/{z}/{x}/{y}"
           f"?ss={max_read}")
    etag = '"' + hashlib.md5(key.encode()).hexdigest() + '"'

    max_tile = 2 ** z - 1
    if not (MIN_ZOOM <= z <= MAX_ZOOM and 0 <= x <= max_tile and 0 <= y <= max_tile):
        return Response(status=404)

    body = render.cache_get(key)
    if body is None:
        if _client_gone():
            return _gone_response()
        try:
            body = render.field_tile(dem, x, y, z, max_read=max_read)
        except Exception as exc:  # noqa: BLE001 — nunca derruba o tile server
            app.logger.warning("campo %s falhou: %s", key, exc)
            return Response(status=503, headers={"Cache-Control": "no-store",
                                                 "Access-Control-Allow-Origin": "*"})
        render.cache_put(key, body)

    return _png_response(body, etag)


@app.get("/terrain/<int:z>/<int:x>/<int:y>.png")
def terrain_tile(z, x, y):
    """Terreno do modo 3D: elevação crua em Terrarium (raster-dem do MapLibre)
    do DEM selecionado — `sp` completa fora da cobertura com FABDEM, `ee` usa o
    FABDEM local (mesmos dados). Independe dos params de cor → cache longo e
    compartilhado. Falha → 0 m SEM cache (max-age=60), nunca transparente
    (transparente = −32768 m no MapLibre)."""
    dem = "sp" if _dem_arg() == "sp" else "fabdem"
    key = f"terr{render.TERRAIN_VERSION}/{dem}/{z}/{x}/{y}"
    etag = '"' + hashlib.md5(key.encode()).hexdigest() + '"'

    max_tile = 2 ** z - 1
    if not (MIN_ZOOM <= z <= min(MAX_ZOOM, render.TERRAIN_MAXZOOM[dem])
            and 0 <= x <= max_tile and 0 <= y <= max_tile):
        return Response(status=404)

    body = render.cache_get(key)
    if body is None:
        if _client_gone():
            return _gone_response()
        try:
            body = render.terrain_tile(dem, x, y, z)
            if body is None:   # oceano OU falha (indistinguíveis) → sem cache
                return _png_response(render.terrain_flat_png(), etag, max_age=60)
            render.cache_put(key, body)
        except Exception as exc:  # noqa: BLE001 — nunca derruba o tile server
            app.logger.warning("terreno %s falhou: %s", key, exc)
            return _png_response(render.terrain_flat_png(), etag, max_age=60)

    return _png_response(body, etag)


@app.get("/osm/<int:z>/<int:x>/<int:y>.png")
def osm_overlay_tile(z, x, y):
    """Camada "Traçado OSM": vias/ferrovias/água do carto padrão com o resto
    transparente (extração por cor em osm_overlay.py) — pra pôr POR CIMA do
    relevo/satélite sem cobrir o fundo. Determinístico por (z,x,y,versão) →
    cache/ETag como os tiles do relevo; falha de rede → transparente SEM
    cachear (mesma convenção das camadas EE)."""
    key = f"osmov{osm_overlay.OSM_OVERLAY_VERSION}/{z}/{x}/{y}"
    etag = '"' + hashlib.md5(key.encode()).hexdigest() + '"'

    max_tile = 2 ** z - 1
    if not (MIN_ZOOM <= z <= min(MAX_ZOOM, osm_overlay.OSM_MAX_ZOOM)
            and 0 <= x <= max_tile and 0 <= y <= max_tile):
        return _png_response(render.transparent_png(), etag)

    body = render.cache_get(key)
    if body is None:
        try:
            body = osm_overlay.render_overlay_tile(z, x, y)
            render.cache_put(key, body)
        except Exception as exc:  # noqa: BLE001 — nunca derruba o tile server
            app.logger.warning("osm overlay %s falhou: %s", key, exc)
            return _png_response(render.transparent_png(), etag, max_age=60)

    return _png_response(body, etag)


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 8400)
    print(f"[cameratopo] http://127.0.0.1:{port}/{{z}}/{{x}}/{{y}}.png")
    # threaded: o dev server do Flask é SERIAL por padrão — com as camadas EE
    # (proxy de ~2 s/tile frio), 30 tiles enfileirados travavam a página local
    # ("as camadas não aparecem"). Em produção o gunicorn já é threaded.
    app.run(host="0.0.0.0", port=port, threaded=True)
