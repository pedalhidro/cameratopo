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
        slopeMax           declividade (m/m) que satura em preto. `auto` = p80.
        slopeGamma         γ do realce de declividade (default 1.2).
        cycles             quantas vezes a paleta se repete na faixa (default 1).
        dem                fabdem (default) | sp (DEM de SP ~5 m).
    GET /health            ok

Sem auth (igual ao resto do projeto — restrinja na borda se precisar). Tiles são
determinísticos por (z,x,y,querystring) → Cache-Control longo + ETag; ponha um
CDN/Cloudflare na frente com a query na chave de cache.
"""

import hashlib
import math
import os

from flask import Flask, Response, jsonify, request

import render

app = Flask(__name__)

MIN_ZOOM = int(os.environ.get("CAMERATOPO_MIN_ZOOM") or 6)
MAX_ZOOM = int(os.environ.get("CAMERATOPO_MAX_ZOOM") or 19)
CACHE_MAX_AGE = int(os.environ.get("CAMERATOPO_MAX_AGE") or 604800)  # 7 dias

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
    gamma = min(5.0, max(0.05, gamma))
    slope_max = max(1e-9, slope_max)

    cyc = request.args.get("cycles")
    try:
        cycles = int(float(cyc)) if cyc else 1
    except (TypeError, ValueError):
        cycles = 1
    cycles = min(16, max(1, cycles))

    return dict(elev_min=elev_min, elev_max=elev_max, slope_max=slope_max,
                gamma=gamma, cycles=cycles)


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


@app.get("/<int:z>/<int:x>/<int:y>.png")
def tile(z, x, y):
    dem = "sp" if (request.args.get("dem") or "").lower() == "sp" else "fabdem"

    # Chave de cache/ETag: z/x/y + params já resolvidos (querystring canônica).
    p = _resolve_params(dem)
    key = (f"{dem}/{z}/{x}/{y}?e={p['elev_min']:.3f},{p['elev_max']:.3f}"
           f"&s={p['slope_max']:.6f}&g={p['gamma']:.3f}&c={p['cycles']}")
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
