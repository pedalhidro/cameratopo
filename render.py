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
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

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

# Versão do RENDERIZADOR. Entra na chave de cache e no ETag do tile: sem ela, um
# tile com os mesmos parâmetros mantém o mesmo ETag depois de mudarmos a
# matemática, o servidor responde 304 e o navegador (e a CDN) seguem servindo o
# PNG antigo — dá pra reiniciar o servidor e continuar vendo o bug. BUMPE isto a
# cada mudança que altere os pixels.
RENDER_VERSION = "4"

# Reamostragem na leitura do DEM. `bilinear` interpola (relevo/declividade suaves)
# em vez do `nearest` default do rio-tiler (que terraça a elevação e serrilha a
# declividade, sobretudo ao ampliar acima da resolução nativa). `average` seria
# ideal no downsampling puro, mas bilinear é o melhor compromisso único.
RESAMPLING = os.environ.get("CAMERATOPO_RESAMPLING") or "bilinear"

# ── Ler/computar SEMPRE ~na resolução nativa do DEM ─────────────────────────
# Resolução nativa aproximada (m/px no solo) de cada fonte. É o que decide em
# QUE resolução ler o DEM e computar a declividade: sempre ~1 pixel por célula
# nativa, e reamplia-se o RGBA já sombreado pro tamanho do tile.
#   • Zoom-IN além do nativo: ler 256 px só INTERPOLA, e a declividade de uma
#     superfície interpolada é constante por célula → aparece como GRADE. Ler no
#     nativo e reampliar o resultado mata a grade (era o que o cliente do amora
#     fazia: declividade no grid nativo do FABDEM).
#   • Zoom-OUT: o overview do COG já entrega poucos bytes; ler no nativo (≤256)
#     também poupa CPU/memória.
FABDEM_NATIVE_M = float(os.environ.get("CAMERATOPO_FABDEM_NATIVE_M") or 30.0)
SP_NATIVE_M = float(os.environ.get("CAMERATOPO_SP_NATIVE_M") or 5.0)
# Piso do grid de leitura: a declividade precisa de alguns pixels. Mantê-lo BAIXO
# é o que evita reintroduzir a grade no zoom extremo — se o piso forçasse ler
# MAIS FINO que o nativo, a declividade voltaria a ver a interpolação por célula.
MIN_READ_SIZE = int(os.environ.get("CAMERATOPO_MIN_READ_SIZE") or 8)

# Teto do grid de leitura = SUPERAMOSTRAGEM no zoom afastado. Um tile de z11 cobre
# ~600 células nativas; ler só 256 obrigava a decimar a elevação ANTES de derivar
# a declividade, o que serrilha (moiré/degraus) e apaga a textura fina. Lendo até
# MAX_READ_SIZE px, a declividade é computada perto do nativo e só então o campo é
# reduzido por MÉDIA de área pro tile — é o que o Earth Engine faz
# (`setDefaultProjection(nativo)` + `ee.Terrain.slope` + pirâmide com reducer mean).
# 512 = 2× supersample: 4× o custo de CPU/leitura, com o grosso do ganho.
MAX_READ_SIZE = int(os.environ.get("CAMERATOPO_MAX_READ_SIZE") or 512)
# Teto absoluto do que a query `ss` pode pedir. O endpoint é público e o custo
# cresce com o quadrado: sem este limite, um `ss` gigante num zoom afastado
# viraria um render caríssimo por tile.
SS_HARD_MAX = int(os.environ.get("CAMERATOPO_SS_HARD_MAX") or 1024)

# Amostragem do percentil de declividade NA RESOLUÇÃO NATIVA (ver
# _slope_pct_native): k×k janelas de SLOPE_WIN_PX px espalhadas pelo bbox. Ler o
# bbox inteiro no nativo seriam milhões de px; 2×2 janelas de 384 px bastam pra um
# p98 estável e mantêm o /stats rápido (é chamado a cada pan no modo auto).
SLOPE_WINDOWS = int(os.environ.get("CAMERATOPO_SLOPE_WINDOWS") or 2)
SLOPE_WIN_PX = int(os.environ.get("CAMERATOPO_SLOPE_WIN_PX") or 384)

# ── Guarda do mosaico FABDEM (1°×1°) ────────────────────────────────────────
# Sem teto de zoom, um tile muito afastado abriria dezenas/centenas de COGs 1°
# (cada abertura = HTTP + memória). O DEM-SP é um COG único (com overviews), não
# precisa de guarda — só o mosaico FABDEM. Acima do span/contagem, o tile sai
# transparente (o cliente simplesmente não mostra relevo tão afastado).
MOSAIC_MAX_SPAN_DEG = float(os.environ.get("CAMERATOPO_MOSAIC_MAX_SPAN") or 6.0)
MOSAIC_MAX_ASSETS = int(os.environ.get("CAMERATOPO_MOSAIC_MAX_ASSETS") or 40)

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


def read_dem_tile(dem, x, y, z, buffer=1, tilesize=256, resampling=None):
    """Lê o tile (z/x/y) do DEM em Web-Mercator com uma borda de `buffer` px pra
    declividade ter vizinhos nas beiradas. Retorna (height, mask) em float64 /
    bool com shape (tilesize+2*buffer,)*2, ou None se nada cobrir o tile.

    `resampling` (default RESAMPLING=bilinear): quem chama passa `average` quando
    a leitura DECIMA a fonte — bilinear pula pixels e serrilha."""
    resampling = resampling or RESAMPLING
    try:
        if dem == "sp":
            with Reader(SAMPA_DEM_URL) as r:
                img = r.tile(x, y, z, tilesize=tilesize, buffer=buffer,
                             resampling_method=resampling)
        else:
            b = TMS.bounds(morecantile.Tile(x, y, z))
            # Guarda: mosaico 1°×1° não serve zoom muito afastado (abriria COGs
            # demais). Acima do span/contagem máximos → sem relevo (transparente).
            if (b.right - b.left) > MOSAIC_MAX_SPAN_DEG or \
               (b.top - b.bottom) > MOSAIC_MAX_SPAN_DEG:
                return None
            assets = _fabdem_assets_for_bounds(b.left, b.bottom, b.right, b.top)
            if not assets or len(assets) > MOSAIC_MAX_ASSETS:
                return None
            img, _ = mosaic_reader(
                assets, _asset_tile, x, y, z,
                tilesize=tilesize, buffer=buffer, resampling_method=resampling,
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
                tilesize=256, max_read=None):
    """Renderiza um tile → bytes PNG (RGBA). Retorna None quando o tile não é
    coberto pelo DEM (o servidor devolve um PNG transparente).

    `max_read` (query `ss`) sobrepõe MAX_READ_SIZE: é o teto da superamostragem
    no zoom afastado — mais px lidos = declividade mais perto do nativo (mais
    textura, menos serrilhado) e mais CPU/rede por tile."""
    b = TMS.bounds(morecantile.Tile(x, y, z))
    lat_c = (b.bottom + b.top) / 2.0
    res256 = _mercator_res_m(z, lat_c)   # m/px de solo se lêssemos tilesize px

    # Grid de leitura ~1 px por célula nativa: nunca MAIS FINO que o nativo (só
    # interpolaria e traria a grade de volta no zoom-in) e — no zoom afastado —
    # até MAX_READ_SIZE px (superamostragem), pra a declividade sair da escala
    # REAL do dado e não de uma elevação já decimada.
    native = SP_NATIVE_M if dem == "sp" else FABDEM_NATIVE_M
    native_px = tilesize * res256 / native      # células nativas ao longo do tile
    cap = int(max_read or MAX_READ_SIZE)        # teto de superamostragem (query `ss`)
    cap = max(MIN_READ_SIZE, min(SS_HARD_MAX, cap))

    # read_size é POTÊNCIA DE 2, e não round(native_px). Isto é o que garante a
    # AUSÊNCIA DE COSTURA: native_px depende da latitude, então arredondá-lo fazia
    # o read_size oscilar entre linhas de tiles vizinhas (37/38 em z15, 299/300 em
    # z12) — grades de leitura diferentes, e a declividade, sendo derivada, dá um
    # degrau na emenda. Truncando pra potência de 2, o read_size fica constante por
    # zoom em faixas largas de latitude (só muda onde native_px cruza uma potência
    # de 2, perto de |lat| 38° e 67°), então tiles vizinhos compartilham a MESMA
    # grade — o buffer faz a declividade da borda casar exatamente com a do vizinho,
    # como se fosse calculada sobre o DEM inteiro.
    # De quebra: 2^k divide/multiplica 256 exatamente → reamostragens exatas, e a
    # resolução da declividade fica ~constante (≈1.0–2.0× o nativo) em todo zoom.
    read_size = _pow2_floor(native_px)          # nunca MAIS FINO que o nativo
    read_size = max(MIN_READ_SIZE, min(_pow2_floor(cap), read_size))

    # Se AINDA estamos decimando de verdade (o teto cortou, então há bem mais
    # células nativas que px lidos), a reamostragem tem que ser por ÁREA: bilinear
    # pula pixels da fonte e serrilha (moiré/degraus). Perto do nativo (a folga de
    # 5% absorve o arredondamento) ou ampliando, bilinear.
    resampling = "average" if native_px > read_size * 1.05 else RESAMPLING

    # Ampliando (read_size < tile) o buffer precisa ter 2 px: a amostragem por
    # coordenada usa 1 vizinho, e o anel externo tem declividade com borda
    # replicada. Reduzindo/no nativo, 1 px basta.
    upsampling = read_size < tilesize
    buf = 2 if upsampling else 1
    read = read_dem_tile(dem, x, y, z, buffer=buf, tilesize=read_size,
                         resampling=resampling)
    if read is None:
        return None
    height, mask = read
    if not mask.any():
        return None

    # Resolução de solo POR PIXEL LIDO = extent / read_size (extent = tilesize*res256).
    res = res256 * (tilesize / read_size)
    slope = compute_slope(height, mask, res, res)   # no array COM buffer

    # Reescala os CAMPOS ESCALARES (elevação + declividade) pro tamanho do tile e
    # SÓ ENTÃO aplica a paleta. Escalares (não RGB) mantêm a paleta cíclica fiel —
    # interpolar RGB entre hues distantes passaria pelo cinza.
    if upsampling:
        # Amostra DENTRO do array bufferizado → sem costura na emenda dos tiles.
        fill = float(np.median(height[mask]))
        h = np.where(np.isfinite(height), height, fill)
        height = _bilinear_from_buffered(h, buf, read_size, tilesize)
        slope = _bilinear_from_buffered(slope, buf, read_size, tilesize)
        mask = _bilinear_from_buffered(mask.astype(np.float64), buf, read_size, tilesize) >= 0.5
    else:
        # Descarta a borda de buffer → read_size×read_size.
        height = height[buf:-buf, buf:-buf]
        mask = mask[buf:-buf, buf:-buf]
        slope = slope[buf:-buf, buf:-buf]
        if read_size != tilesize:
            # Reduzindo: média de ÁREA (BOX) — cada pixel de saída cobre exatamente
            # a sua área dentro do tile, então também não cria costura. É a mesma
            # agregação da pirâmide do Earth Engine (reducer `mean`).
            fill = float(np.median(height[mask])) if mask.any() else 0.0
            height = _resize_scalar(height, tilesize, fill)
            slope = _resize_scalar(slope, tilesize, 0.0)
            mask = _resize_mask(mask, tilesize)

    rgba = shade(height, mask, slope, elev_min, elev_max, slope_max, gamma, cycles)
    return _png_bytes(rgba)


def _pow2_floor(v):
    """Maior potência de 2 ≤ v (mínimo 1)."""
    return 1 << int(math.floor(math.log2(v))) if v >= 1.0 else 1


def _bilinear_from_buffered(a, buf, read_size, tilesize):
    """Amostra o array COM BUFFER ((R+2B)²) nas posições dos centros dos pixels do
    tile → (T,T), por coordenada geográfica.

    É o que evita COSTURA entre tiles no zoom-in: recortar o buffer ANTES de
    ampliar faria a interpolação grampear na borda do array, e cada tile
    interpolaria isolado (degrau visível na emenda). Amostrando dentro do array
    bufferizado, os pixels da beirada enxergam os vizinhos reais do tile ao lado —
    e como as grades de leitura de tiles vizinhos são contíguas e alinhadas, os
    dois chegam ao MESMO valor na fronteira.

    Centro do pixel de saída j ↔ índice u = B - 0.5 + (j+0.5)·R/T. Com B=2 o
    intervalo amostrado exclui o anel externo (onde compute_slope replicou a
    borda), então a declividade também casa entre tiles."""
    B, R, T = buf, read_size, tilesize
    j = np.arange(T)
    u = B - 0.5 + (j + 0.5) * (R / T)
    u0 = np.clip(np.floor(u).astype(np.int64), 0, a.shape[0] - 2)
    f = u - u0
    r0, r1 = u0, u0 + 1
    fr, fc = f[:, None], f[None, :]
    a00 = a[np.ix_(r0, r0)]; a01 = a[np.ix_(r0, r1)]
    a10 = a[np.ix_(r1, r0)]; a11 = a[np.ix_(r1, r1)]
    return (a00 * (1 - fr) * (1 - fc) + a01 * (1 - fr) * fc
            + a10 * fr * (1 - fc) + a11 * fr * fc)


def _rescale_filter(src, size):
    """BOX (média de área) ao REDUZIR — antisserrilhado, é a agregação `mean` da
    pirâmide do GEE. BILINEAR ao AMPLIAR."""
    return Image.BOX if size < src else Image.BILINEAR


def _resize_scalar(field, size, fill):
    """Reescala um campo escalar (H,W) float pra size×size. nodata (nan) vira
    `fill` antes, pra não propagar nan na interpolação — o alfa final vem da
    máscara reescalada à parte."""
    a = np.where(np.isfinite(field), field, fill).astype(np.float32)
    img = Image.fromarray(a, mode="F").resize((size, size), _rescale_filter(a.shape[0], size))
    return np.asarray(img, dtype=np.float64)


def _resize_mask(mask, size):
    """Reescala a máscara (bool) pra size×size; ≥0.5 = coberto."""
    m = mask.astype(np.float32)
    img = Image.fromarray(m, mode="F").resize((size, size), _rescale_filter(m.shape[0], size))
    return np.asarray(img) >= 0.5


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
    kw = dict(dst_crs="EPSG:4326", bounds_crs="EPSG:4326", max_size=max_size,
              resampling_method=RESAMPLING)
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


def _slope_pct_native(dem, bbox, pct=98.0):
    """p{pct} da declividade calculada na resolução NATIVA do DEM.

    A declividade DEPENDE DA ESCALA: derivá-la de um DEM já decimado (como a
    leitura de elevação faz, ~217 m/px numa viewport de z11) subestima o valor em
    ~2×, enquanto o render deriva a declividade perto do nativo. O resultado era
    um slopeMax pequeno demais → tudo saturava em preto e o ruído de declividade
    das áreas planas virava "grade". O Earth Engine tira o percentil da
    declividade NATIVA (`ee.Terrain.slope` sobre a projeção nativa); é o que
    fazemos aqui, amostrando janelas nativas em vez de ler o bbox inteiro (que
    seriam milhões de pixels). Devolve None se nada puder ser lido."""
    native = SP_NATIVE_M if dem == "sp" else FABDEM_NATIVE_M
    w, s, e, n = bbox
    lat_c = (s + n) / 2.0
    mx = 111320.0 * math.cos(math.radians(lat_c))
    px_w = (e - w) * mx / native
    px_h = (n - s) * 111320.0 / native

    if max(px_w, px_h) <= SLOPE_WIN_PX * 1.5:
        wins = [bbox]                       # bbox pequeno: lê inteiro, já é nativo
    else:                                   # grande: amostra k×k janelas nativas
        k = max(1, SLOPE_WINDOWS)
        dw, dh = SLOPE_WIN_PX * native / mx, SLOPE_WIN_PX * native / 111320.0
        wins = [(w + (e - w) * (i + 0.5) / k - dw / 2,
                 s + (n - s) * (j + 0.5) / k - dh / 2,
                 w + (e - w) * (i + 0.5) / k + dw / 2,
                 s + (n - s) * (j + 0.5) / k + dh / 2)
                for i in range(k) for j in range(k)]

    def _win_slope(wb):
        r = _read_dem_part(dem, wb, max_size=SLOPE_WIN_PX)
        if r is None:
            return None
        h, m = r
        if not m.any():
            return None
        H, W = h.shape
        lc = (wb[1] + wb[3]) / 2.0
        rx = ((wb[2] - wb[0]) / W) * 111320.0 * math.cos(math.radians(lc))
        ry = ((wb[3] - wb[1]) / H) * 111320.0
        return compute_slope(h, m, rx, ry)[m]

    # As janelas são I/O de COG remoto (o GIL é liberado): lê em paralelo, senão
    # o /stats — chamado a cada pan no modo auto — ficaria lento demais.
    if len(wins) == 1:
        vals = [v for v in (_win_slope(wins[0]),) if v is not None]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(wins))) as ex:
            vals = [v for v in ex.map(_win_slope, wins) if v is not None]
    if not vals:
        return None
    return max(1e-9, float(np.percentile(np.concatenate(vals), pct)))


def stats_for_bbox(dem, bbox):
    """{elevMin(p5), elevMax(p80), slopeMax(p98 da declividade NATIVA)} sobre um
    bbox geográfico (oeste, sul, leste, norte, em graus), ou None se a leitura
    falhar / não houver cobertura. NÃO cacheia (o bbox é livre).

    A declividade sai da escala NATIVA (ver _slope_pct_native) — antes vinha do
    DEM decimado e saía ~2× menor, o que saturava o relevo em preto. Elevação
    segue em p5/p80 (o app de Earth Engine usa p2/p98, mas p5/p80 é o que dá o
    contraste de cor atual).

    É a mesma matemática que o modo `auto` usa; o modo "auto segue a tela" da UI
    chama isto pela viewport corrente e congela os números explícitos na
    querystring, então continua uniforme (sem costura) por toda a grade — só que
    adaptado ao que está na tela."""
    read = _read_dem_part(dem, bbox)
    if read is None:
        return None
    height, mask = read
    if not mask.any():
        return None
    hv = height[mask]

    slope_max = _slope_pct_native(dem, bbox, 98.0)
    if slope_max is None:                   # fallback: escala decimada (subestima)
        w, s, e, n = bbox
        H, W = height.shape
        lat_c = (s + n) / 2.0
        res_x = ((e - w) / W) * 111320.0 * math.cos(math.radians(lat_c))
        res_y = ((n - s) / H) * 111320.0
        sv = compute_slope(height, mask, res_x, res_y)[mask]
        slope_max = max(1e-9, float(np.percentile(sv, 98)))

    return {
        "elevMin": float(np.percentile(hv, 5)),
        "elevMax": float(np.percentile(hv, 80)),
        "slopeMax": slope_max,
    }


def auto_stats(dem):
    """{elevMin(p5), elevMax(p80), slopeMax(p98 da declividade)} sobre a região
    de referência. Calculado uma vez por DEM e cacheado — assim `auto` é
    constante em toda a grade de tiles (sem costuras)."""
    # Resolve DENTRO do lock (double-checked): numa carga fria com várias threads
    # (gunicorn --threads 8), todas pegam o MESMO resultado — real OU fallback —
    # em vez de umas lerem telhas com sucesso e outras caírem no fallback, o que
    # normalizaria tiles vizinhos diferente (costura). Segura o lock durante a
    # leitura (custo único por DEM, só no cold start). O fallback NÃO é cacheado:
    # a próxima onda tenta os percentis reais de novo.
    with _auto_lock:
        if dem in _auto_cache:
            return _auto_cache[dem]
        stats = stats_for_bbox(dem, AUTO_BBOX)
        if stats is None:
            return {"elevMin": 720.0, "elevMax": 920.0, "slopeMax": 0.20}
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
