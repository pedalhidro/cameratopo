"""Renderizador da Câmera Topográfica como tiles XYZ (z/x/y.png).

Porta pro servidor a MESMA matemática que `web/app.js` faz no cliente
(`computeSlope` + `renderReliefToDataURL`): elevação na paleta cmocean.phase
(cíclica, perceptual) multiplicada por um realce de declividade branco→preto
γ-corrigido. A diferença é que aqui cada tile Web-Mercator é renderizado
independentemente a partir dos COGs do FABDEM / DEM de SP (hospedados no
telhas), o que exige que os parâmetros de faixa (elevMin/elevMax/slopeMax) sejam
CONSTANTES em toda a grade de tiles — senão cada tile normalizaria diferente e
apareceriam costuras. Por isso eles vêm da querystring (ver server.py); o valor
`auto` é resolvido UMA vez sobre uma região de referência fixa e cacheado, então
continua uniforme.

Puro desenho + leitura read-only de COG via rio-tiler/GDAL (/vsicurl/). Nada
aqui toca estado do amora.
"""

from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict

import numpy as np
from PIL import Image
from rio_tiler.errors import EmptyMosaicError, TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.mosaic import mosaic_reader
import morecantile

# ── Fontes de DEM (mesmas URLs que o cliente usa) ────────────────────────────
FABDEM_BASE_URL = "https://telhas.pedalhidrografi.co/fabdem/"
SAMPA_DEM_URL = "https://telhas.pedalhidrografi.co/dem/sampa_geral.tif"

# Região de referência p/ o modo `auto` (Região Metropolitana de São Paulo).
# Os percentis são calculados aqui uma vez e reusados por toda a grade de tiles.
AUTO_BBOX = (-47.3, -24.15, -45.9, -23.15)  # (oeste, sul, leste, norte) em graus

TMS = morecantile.tms.get("WebMercatorQuad")

# Paleta cmocean.phase (17 âncoras RGB), idêntica à CMO_PHASE do app.js. É
# cíclica (primeira == última âncora), então repetir N ciclos não emenda.
CMO_PHASE = np.array([
    [168, 120, 13], [190, 104, 40], [207, 86, 67], [219, 64, 102],
    [223, 42, 147], [213, 41, 196], [192, 65, 229], [162, 92, 243],
    [125, 115, 240], [82, 133, 220], [44, 144, 188], [25, 149, 156],
    [12, 152, 124], [36, 154, 82], [94, 148, 32], [139, 134, 13],
    [168, 120, 13],
], dtype=np.float64)


def fabdem_tile_name(lat_lo: int, lon_lo: int) -> str:
    """Convenção do bucket: canto SW, hemisfério antes dos dígitos.
    lat=-24, lon=-47 → S24W047_FABDEM_V1-2.tif"""
    ns = "N" if lat_lo >= 0 else "S"
    ew = "E" if lon_lo >= 0 else "W"
    return f"{ns}{abs(lat_lo):02d}{ew}{abs(lon_lo):03d}_FABDEM_V1-2.tif"


def _fabdem_assets_for_bounds(west, south, east, north):
    """URLs dos COGs FABDEM 1°×1° que intersectam um bbox geográfico."""
    assets = []
    for lat_lo in range(math.floor(south), math.floor(north) + 1):
        for lon_lo in range(math.floor(west), math.floor(east) + 1):
            assets.append(FABDEM_BASE_URL + fabdem_tile_name(lat_lo, lon_lo))
    return assets


# ── Leitura de DEM ───────────────────────────────────────────────────────────

def _asset_tile(asset, x, y, z, **kwargs):
    """Lê um tile de um COG; qualquer falha (404 em oceano, timeout) vira
    TileOutsideBounds pro mosaic_reader simplesmente pular o asset."""
    try:
        with Reader(asset) as r:
            return r.tile(x, y, z, **kwargs)
    except TileOutsideBounds:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TileOutsideBounds(str(exc)) from exc


def read_dem_tile(dem, x, y, z, buffer=1, tilesize=256):
    """Lê o tile (z/x/y) do DEM em Web-Mercator com uma borda de `buffer` px pra
    declividade ter vizinhos nas beiradas. Retorna (height, mask) em float64 /
    bool com shape (tilesize+2*buffer,)*2, ou None se nada cobrir o tile."""
    try:
        if dem == "sp":
            with Reader(SAMPA_DEM_URL) as r:
                img = r.tile(x, y, z, tilesize=tilesize, buffer=buffer)
        else:
            b = TMS.bounds(morecantile.Tile(x, y, z))
            assets = _fabdem_assets_for_bounds(b.left, b.bottom, b.right, b.top)
            if not assets:
                return None
            img, _ = mosaic_reader(
                assets, _asset_tile, x, y, z,
                tilesize=tilesize, buffer=buffer,
                allowed_exceptions=(TileOutsideBounds,),
            )
    except (TileOutsideBounds, EmptyMosaicError):
        return None
    except Exception:  # noqa: BLE001 — mosaico vazio / todos os assets falharam
        return None

    band = img.array[0]  # MaskedArray (H, W)
    height = np.ma.filled(band, np.nan).astype(np.float64)
    if band.mask is np.ma.nomask:
        mask = np.isfinite(height)
    else:
        mask = (~band.mask) & np.isfinite(height)
    return height, mask


# ── Matemática (idêntica ao cliente) ─────────────────────────────────────────

def compute_slope(height, mask, res_x_m, res_y_m):
    """Declividade (m/m) por diferença central. Vizinho nodata cai na própria
    altura (gradiente zero em vez de salto fictício). Bordas replicam."""
    h = np.where(mask, height, np.nan)

    def neigh(shift_r, shift_c):
        n = np.roll(h, (shift_r, shift_c), axis=(0, 1))
        # Replica a borda (o roll faz wrap; corrige a linha/coluna que vazou).
        if shift_r == 1:
            n[0, :] = h[0, :]
        elif shift_r == -1:
            n[-1, :] = h[-1, :]
        if shift_c == 1:
            n[:, 0] = h[:, 0]
        elif shift_c == -1:
            n[:, -1] = h[:, -1]
        return n

    hn = neigh(1, 0)   # norte  (linha - 1)
    hs = neigh(-1, 0)  # sul    (linha + 1)
    hw = neigh(0, 1)   # oeste  (col - 1)
    he = neigh(0, -1)  # leste  (col + 1)
    # Vizinho nodata (nan) → usa a própria altura.
    hn = np.where(np.isnan(hn), h, hn)
    hs = np.where(np.isnan(hs), h, hs)
    hw = np.where(np.isnan(hw), h, hw)
    he = np.where(np.isnan(he), h, he)

    dhdx = (he - hw) / (2.0 * res_x_m)
    dhdy = (hs - hn) / (2.0 * res_y_m)
    slope = np.sqrt(dhdx * dhdx + dhdy * dhdy)
    return np.where(mask, slope, 0.0)


def _phase_rgb(t):
    """Mapeia t∈[0,1) → RGB interpolando as âncoras da cmocean.phase.
    Vetorizado: t com shape (...,) → saída (..., 3)."""
    n = CMO_PHASE.shape[0] - 1  # 16 segmentos
    f = np.clip(t, 0.0, 1.0) * n
    k = np.clip(np.floor(f).astype(np.int64), 0, n - 1)
    frac = (f - k)[..., None]
    a = CMO_PHASE[k]
    b = CMO_PHASE[k + 1]
    return a + (b - a) * frac


def shade(height, mask, slope, elev_min, elev_max, slope_max, gamma, cycles):
    """Compõe o RGBA (uint8) do relevo. cycles = quantas vezes a paleta se
    repete ao longo da faixa de elevação (contorno cíclico)."""
    H, W = height.shape
    rgba = np.zeros((H, W, 4), dtype=np.uint8)

    elev_span = elev_max - elev_min
    slope_max = max(1e-9, slope_max)
    inv_gamma = 1.0 / max(0.05, gamma or 1.2)
    cycles = max(1, int(cycles or 1))

    if elev_span > 0:
        t = (height - elev_min) / elev_span            # posição na faixa
        t = np.mod(np.clip(t, 0.0, 1.0) * cycles, 1.0)  # ciclagem sem emenda
    else:
        t = np.full((H, W), 0.5)
    rgb = _phase_rgb(t)  # (H, W, 3)

    # Declividade como multiplicador branco→preto γ-corrigido.
    s_norm = np.minimum(1.0, slope / slope_max)
    slope_factor = (1.0 - np.power(s_norm, inv_gamma))[..., None]

    shaded = np.clip(rgb * slope_factor, 0, 255).astype(np.uint8)
    rgba[..., :3] = shaded
    rgba[..., 3] = np.where(mask, 255, 0).astype(np.uint8)
    return rgba


def _mercator_res_m(z, lat_deg):
    """Resolução de solo (m/px) do Web-Mercator no zoom z, na latitude dada."""
    return 156543.03392804097 * math.cos(math.radians(lat_deg)) / (2.0 ** z)


def render_tile(dem, x, y, z, *, elev_min, elev_max, slope_max, gamma, cycles,
                tilesize=256):
    """Renderiza um tile → bytes PNG (RGBA). Retorna None quando o tile não é
    coberto pelo DEM (o servidor devolve um PNG transparente)."""
    read = read_dem_tile(dem, x, y, z, buffer=1, tilesize=tilesize)
    if read is None:
        return None
    height, mask = read
    if not mask.any():
        return None

    b = TMS.bounds(morecantile.Tile(x, y, z))
    lat_c = (b.bottom + b.top) / 2.0
    res = _mercator_res_m(z, lat_c)  # px quadrado no Web-Mercator local
    slope = compute_slope(height, mask, res, res)

    rgba = shade(height, mask, slope, elev_min, elev_max, slope_max, gamma, cycles)
    # Descarta a borda de buffer (1 px de cada lado) → tilesize×tilesize.
    rgba = rgba[1:-1, 1:-1, :]
    return _png_bytes(rgba)


def _png_bytes(rgba):
    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def transparent_png(tilesize=256):
    """PNG transparente de 1 tile (fora de cobertura / zoom fora de faixa)."""
    return _png_bytes(np.zeros((tilesize, tilesize, 4), dtype=np.uint8))


# ── Modo `auto`: percentis sobre a região de referência (cacheado) ───────────

_auto_cache: dict[str, dict] = {}
_auto_lock = threading.Lock()


def _asset_part(asset, bbox, **kwargs):
    try:
        with Reader(asset) as r:
            return r.part(bbox, **kwargs)
    except TileOutsideBounds:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TileOutsideBounds(str(exc)) from exc


def _read_dem_part(dem, bbox, max_size=512):
    """Lê a região de referência em EPSG:4326 (graus), decimada a ~max_size.
    Retorna None se a leitura falhar (telhas fora do ar / sem cobertura)."""
    w, s, e, n = bbox
    kw = dict(dst_crs="EPSG:4326", bounds_crs="EPSG:4326", max_size=max_size)
    try:
        if dem == "sp":
            with Reader(SAMPA_DEM_URL) as r:
                img = r.part(bbox, **kw)
        else:
            assets = _fabdem_assets_for_bounds(w, s, e, n)
            if not assets:
                return None
            img, _ = mosaic_reader(
                assets, _asset_part, bbox, **kw,
                allowed_exceptions=(TileOutsideBounds,),
            )
    except (TileOutsideBounds, EmptyMosaicError):
        return None
    except Exception:  # noqa: BLE001
        return None
    band = img.array[0]
    height = np.ma.filled(band, np.nan).astype(np.float64)
    if band.mask is np.ma.nomask:
        mask = np.isfinite(height)
    else:
        mask = (~band.mask) & np.isfinite(height)
    return height, mask


def auto_stats(dem):
    """{elevMin(p5), elevMax(p80), slopeMax(p80 da declividade)} sobre a região
    de referência. Calculado uma vez por DEM e cacheado — assim `auto` é
    constante em toda a grade de tiles (sem costuras)."""
    with _auto_lock:
        if dem in _auto_cache:
            return _auto_cache[dem]
    read = _read_dem_part(dem, AUTO_BBOX)
    stats = None
    if read is not None:
        height, mask = read
        if mask.any():
            w, s, e, n = AUTO_BBOX
            H, W = height.shape
            lat_c = (s + n) / 2.0
            res_x = ((e - w) / W) * 111320.0 * math.cos(math.radians(lat_c))
            res_y = ((n - s) / H) * 111320.0
            slope = compute_slope(height, mask, res_x, res_y)
            hv = height[mask]
            sv = slope[mask]
            stats = {
                "elevMin": float(np.percentile(hv, 5)),
                "elevMax": float(np.percentile(hv, 80)),
                "slopeMax": max(1e-9, float(np.percentile(sv, 80))),
            }
    if stats is None:
        # Fallback sensato pra RMSP se a leitura falhar (offline/telhas fora).
        # NÃO cacheia — a próxima requisição tenta calcular os percentis reais.
        return {"elevMin": 720.0, "elevMax": 920.0, "slopeMax": 0.20}
    with _auto_lock:
        _auto_cache[dem] = stats
    return stats


# ── Cache LRU de PNGs renderizados (chave = z/x/y + params) ──────────────────

_png_cache: "OrderedDict[str, bytes]" = OrderedDict()
_png_cache_lock = threading.Lock()
PNG_CACHE_MAX = 512


def cache_get(key):
    with _png_cache_lock:
        if key in _png_cache:
            _png_cache.move_to_end(key)
            return _png_cache[key]
    return None


def cache_put(key, value):
    with _png_cache_lock:
        _png_cache[key] = value
        while len(_png_cache) > PNG_CACHE_MAX:
            _png_cache.popitem(last=False)


# ── Smoke test offline (sem rede): valida a matemática de shade/slope ────────
if __name__ == "__main__":
    # DEM sintético: um cone — elevação radial, declividade crescente pra fora.
    N = 64
    yy, xx = np.mgrid[0:N, 0:N]
    height = 1000.0 - np.sqrt((xx - N / 2) ** 2 + (yy - N / 2) ** 2) * 8.0
    mask = np.ones((N, N), dtype=bool)
    slope = compute_slope(height, mask, 30.0, 30.0)
    for cyc in (1, 3):
        rgba = shade(height, mask, slope, height.min(), height.max(),
                     float(slope.max()), 1.2, cyc)
        png = _png_bytes(rgba)
        assert rgba.shape == (N, N, 4)
        assert (rgba[..., 3] == 255).all()
        print(f"cycles={cyc}: RGBA ok, PNG {len(png)} bytes, "
              f"slope[max]={slope.max():.4f}")
    print("smoke test ok")
