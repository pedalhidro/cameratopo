"""Servidor XYZ da Câmera Topográfica (projeto paralelo, tipo o eink/).

Serve tiles PNG do relevo em `GET /{z}/{x}/{y}.png` lendo os COGs do FABDEM /
DEM de SP (read-only, /vsicurl/). DELIBERADAMENTE fora do backend do amora —
não toca catálogo/estado; roda em qualquer lugar (laptop, Cloud Run próprio).

    pip install -r cameratopo/requirements.txt
    python cameratopo/server.py            # http://127.0.0.1:8400

Endpoint:
    GET /{z}/{x}/{y}.png   tile do relevo. Query params (todos opcionais):
        elevMin, elevMax   faixa de elevação da paleta, em metros. `auto` (ou
                           omitido) = percentil p5 / p80 da região de referência.
        slopeMax           declividade (m/m) que satura em preto. `auto` = p98.
        slopeGamma         γ do realce de declividade (default 1.2).
        cycles             quantas vezes a paleta se repete na faixa (default 1).
        dem                fabdem (default) | sp (DEM de SP ~5 m).
        ss                 teto da superamostragem no zoom afastado, em px por
                           lado (default 512, máx 1024). Mais = declividade mais
                           perto do nativo (mais textura), mais CPU/rede.
    GET /health            ok

Sem auth (igual ao resto do projeto — restrinja na borda se precisar). Tiles são
determinísticos por (z,x,y,querystring) → Cache-Control longo + ETag; ponha um
CDN/Cloudflare na frente com a query na chave de cache.
"""

import hashlib
import math
import os

from flask import Flask, Response, jsonify, request, send_from_directory

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


def _resolve_params(dem):
    """Resolve os params da query, substituindo `auto`/ausente pelos percentis
    cacheados da região de referência. Retorna dict pronto pro render."""
    elev_min = _fnum("elevMin")
    elev_max = _fnum("elevMax")
    slope_max = _fnum("slopeMax")

    if elev_min is None or elev_max is None or slope_max is None:
        st = render.auto_stats(dem)
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

    return dict(elev_min=elev_min, elev_max=elev_max, slope_max=slope_max,
                gamma=gamma, cycles=cycles, max_read=max_read)


def _png_response(body, etag):
    inm = request.headers.get("If-None-Match")
    headers = {
        "Content-Type": "image/png",
        "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
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
    (sem costura). Query: bbox=oeste,sul,leste,norte (graus) & dem=fabdem|sp."""
    dem = "sp" if (request.args.get("dem") or "").lower() == "sp" else "fabdem"
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
    dem = "sp" if (request.args.get("dem") or "").lower() == "sp" else "fabdem"

    # Chave de cache/ETag: z/x/y + params já resolvidos (querystring canônica).
    p = _resolve_params(dem)
    key = (f"{dem}/{z}/{x}/{y}?e={p['elev_min']:.3f},{p['elev_max']:.3f}"
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
        try:
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 8400)
    print(f"[cameratopo] http://127.0.0.1:{port}/{{z}}/{{x}}/{{y}}.png")
    app.run(host="0.0.0.0", port=port)
