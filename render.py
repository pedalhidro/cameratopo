"""Renderizador da Câmera Topográfica como tiles XYZ (z/x/y.png).

Porta pro servidor a MESMA matemática que `web/app.js` faz no cliente
(`computeSlope` + `renderReliefToDataURL`): elevação na paleta cmocean.phase
(cíclica, perceptual) multiplicada por um realce de declividade branco→preto
γ-corrigido. A diferença é que aqui cada tile Web-Mercator é renderizado
independentemente a partir dos COGs do FABDEM (R2, fabdem.pedalhidrografi.co) /
DEM de SP (telhas), o que exige que os parâmetros de faixa
(elevMin/elevMax/slopeMax) sejam
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
FABDEM_BASE_URL = "https://fabdem.pedalhidrografi.co/"
# Direto do bucket (não pelo domínio telhas.*, que tem LB/CDN na frente): bucket
# e Cloud Run na MESMA região (southamerica-east1) → transferência grátis e
# latência menor. Mesmo arquivo → pixels iguais (sem bump de versão).
SAMPA_DEM_URL = os.environ.get("CAMERATOPO_SAMPA_URL") or \
    "https://storage.googleapis.com/telhas/dem/sampa_geral.tif"
# O COG do DEM-SP não declara nodata: fora da cobertura o valor é 0 (e uns
# resíduos ~1e-9), e o retângulo tem uma faixa de zeros na borda. Sem isto a
# reamostragem mistura 0 m com 700 m e a borda vira um aro laranja/preto
# (cor de wrap da paleta + "penhasco" de declividade). nodata=0 faz o GDAL
# mascarar/ignorar esses pixels na reamostragem; o piso pega os resíduos.
SAMPA_READER_OPTS = {"nodata": 0}
SAMPA_MIN_VALID_M = 1.0

# Região de referência p/ o modo `auto` (Região Metropolitana de São Paulo).
# Os percentis são calculados aqui uma vez e reusados por toda a grade de tiles.
AUTO_BBOX = (-47.3, -24.15, -45.9, -23.15)  # (oeste, sul, leste, norte) em graus

TMS = morecantile.tms.get("WebMercatorQuad")

# Versão do RENDERIZADOR. Entra na chave de cache e no ETag do tile: sem ela, um
# tile com os mesmos parâmetros mantém o mesmo ETag depois de mudarmos a
# matemática, o servidor responde 304 e o navegador (e a CDN) seguem servindo o
# PNG antigo — dá pra reiniciar o servidor e continuar vendo o bug. BUMPE isto a
# cada mudança que altere os pixels — E o TILE_VERSION do web/index.html junto
# (ele vai na URL do tile como cache-buster; o ETag sozinho não fura o max-age
# de 7 dias do navegador/CDN).
RENDER_VERSION = "9"

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
# 49 = 7×7: cobre QUALQUER tile de z6 (≤ 5,6° de lado). Com 40, os tiles de z6
# que tocavam 7×7 COGs saíam vazios e z6 (e z5 em retina) virava um xadrez de
# buracos. Medido: tile z6 de 42 COGs ≈ 3,5 s; z6 inteiro são só 4096 tiles, e a
# CDN guarda. z ≤ 5 continua vazio (≥ 11° de lado, ~130 COGs) — a UI avisa.
MOSAIC_MAX_ASSETS = int(os.environ.get("CAMERATOPO_MOSAIC_MAX_ASSETS") or 49)

# ── Tiers de resolução reduzida (FABDEM agregado no Earth Engine) ──────────
# Zoom afastado sobre o mosaico 1°×1° = dezenas/centenas de COGs por tile (caro,
# ou vazio pela guarda acima). Cada tier é UM grid EPSG:4326 em poucos arquivos
# grandes com overviews (tools/export_fabdem_tier.py): banda 1 = média da
# elevação, banda 2 = média da declividade NATIVA (tan ×10000) — a declividade
# já vem da resolução nativa, então aqui NÃO se deriva nada (sem buffer, sem
# costura: cada pixel de saída é a média de área do seu pedaço).
# Escolha por tile: o tier MAIS GROSSO que ainda tem ≥ `ss` px de lado no tile
# (resolução de sobra) e que CONTÉM o tile inteiro; senão o mosaico nativo.
# Com ss=512: z ≤ 7 → 500 m (globo); z8–9 → 90 m (América do Sul); resto 30 m.
# Terreno 3D idem, com 256 px.
# Lidos DIRETO do bucket (storage.googleapis.com), não pelo domínio telhas.*
# (LB/CDN): o bucket `telhas` e o Cloud Run estão os dois em southamerica-east1
# → transferência GCS→Cloud Run na mesma região é grátis e de baixa latência.
# (O EE só exporta pra GCS/Drive/asset — por isso os tiers não estão no R2.)
_TELHAS_DEM = os.environ.get("CAMERATOPO_TIER_BASE") or "https://storage.googleapis.com/telhas/dem"
TIERS = [   # do mais grosso pro mais fino
    {"name": "fabdem_500m", "ppd": 240, "file_deg": 90, "origin": (-180.0, 90.0),
     "extent": (-180.0, -90.0, 180.0, 90.0)},
    {"name": "fabdem_90m_sa", "ppd": 1200, "file_deg": 10, "origin": (-90.0, 20.0),
     "extent": (-90.0, -60.0, -30.0, 20.0)},
]
TIER_SLOPE_SCALE = 10000.0
# DESLIGADO até os arquivos do export existirem no bucket: com tier ligado e
# arquivo faltando, todo tile de zoom afastado vira DEMReadError. Ligar = trocar
# o default + bump RENDER/TILE_VERSION e TERRAIN_VERSION (os tiles de zoom
# afastado renderizados sem tier estão em cache de 7 dias).
TIER_ON = (os.environ.get("CAMERATOPO_TIER") or "0") != "0"


def pick_tier(x, y, z, read_px):
    """Tier pro tile (dict) ou None (→ mosaico nativo)."""
    if not TIER_ON:
        return None
    b = TMS.bounds(morecantile.Tile(x, y, z))
    for t in TIERS:
        if (360.0 / (2 ** z)) * t["ppd"] < read_px:
            continue                                   # grosso demais pra este zoom
        w, s_, e, n = t["extent"]
        if b.left >= w and b.right <= e and b.bottom >= s_ and b.top <= n:
            return t
    return None


def _tier_assets_for_bounds(t, west, south, east, north):
    """Arquivos do tier (EE: <nome>-<linha px>-<coluna px>.tif, a partir da
    origem NO) que tocam o bbox."""
    ox, oy = t["origin"]
    fd, fpx = t["file_deg"], int(round(t["file_deg"] * t["ppd"]))
    w, s_, e, n = t["extent"]
    out = []
    for r in range(int(round((oy - s_) / fd))):
        top = oy - r * fd
        if north <= top - fd or south >= top:
            continue
        for c in range(int(round((e - ox) / fd))):
            left = ox + c * fd
            if east <= left or west >= left + fd:
                continue
            out.append(f"{_TELHAS_DEM}/{t['name']}/{t['name']}-{r * fpx:010d}-{c * fpx:010d}.tif")
    return out


def _tier_read(t, x, y, z, tilesize, bands):
    """Lê `bands` do tier no tile (média de área) → MaskedArray (B,H,W), ou None."""
    b = TMS.bounds(morecantile.Tile(x, y, z))
    assets = _tier_assets_for_bounds(t, b.left, b.bottom, b.right, b.top)
    if not assets:
        return None
    img, failed = _mosaic_tile(assets, x, y, z, tilesize=tilesize, indexes=bands,
                               resampling_method="average", reproject_method="average")
    if failed or img is None:
        return None     # quem chama levanta DEMReadError (tier cobre a extensão toda)
    return img.array


def _tier_fields(t, x, y, z, tilesize):
    """Campos do tier. Nodata do tier = mar → 0 m, declividade 0 (o grid cobre a
    extensão inteira; buraco não é falta de arquivo). Leitura falhou → erro."""
    a = _tier_read(t, x, y, z, tilesize, (1, 2))
    if a is None:
        raise DEMReadError(f"tier {t['name']} {z}/{x}/{y}: leitura falhou")
    m = ~np.ma.getmaskarray(a[0])
    height = np.where(m, np.ma.filled(a[0], 0).astype(np.float64), 0.0)
    slope = np.where(m, np.ma.filled(a[1], 0).astype(np.float64) / TIER_SLOPE_SCALE, 0.0)
    return height, np.ones_like(m), slope


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


# Células 1°×1° que TÊM arquivo FABDEM (fabdem_cells.txt, da coleção do EE).
# Célula fora da lista é OCEANO com certeza — vira 0 m sem nenhum pedido (antes:
# um 404 por célula de mar em todo tile costeiro). Célula DA lista que falha na
# leitura é FALHA (DEMReadError, não cacheia) — não "mar": sem essa distinção,
# um soluço do R2 viraria um remendo plano a 0 m em terra, cacheado 7 dias.
def _load_cells():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fabdem_cells.txt")
    cells = set()
    with open(p) as f:
        for line in f:
            c = line.strip()
            if not c or c.startswith("#"):
                continue
            lat = int(c[1:3]) * (1 if c[0] == "N" else -1)
            lon = int(c[4:7]) * (1 if c[3] == "E" else -1)
            cells.add((lat, lon))
    return cells


FABDEM_CELLS = _load_cells()


class DEMReadError(RuntimeError):
    """Leitura de DEM que DEVIA ter dado certo falhou (rede/R2) — não é "sem
    dado": quem chama não pode cachear o resultado como vazio/mar."""


def _fabdem_assets_for_bounds(west, south, east, north):
    """URLs dos COGs FABDEM 1°×1° que EXISTEM e intersectam um bbox geográfico."""
    # ε: borda de tile exatamente em grau inteiro (−45.0 sai −45.000000000001)
    # não pode puxar a célula vizinha — ela responderia TileOutsideBounds à toa
    e9 = 1e-9
    assets = []
    for lat_lo in range(math.floor(south + e9), math.floor(north - e9) + 1):
        for lon_lo in range(math.floor(west + e9), math.floor(east - e9) + 1):
            if (lat_lo, lon_lo) in FABDEM_CELLS:
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


def _mosaic_tile(assets, x, y, z, **kwargs):
    """mosaic_reader que SEPARA "tile fora deste COG" (legítimo, pula) de FALHA
    de leitura (rede/R2/404 de arquivo que devia existir). Devolve (img ou None
    se nenhum COG cobriu, lista de COGs que falharam)."""
    failed = []

    def one(asset, x, y, z, **kw):
        try:
            with Reader(asset) as r:
                return r.tile(x, y, z, **kw)
        except TileOutsideBounds:
            raise
        except Exception as exc:  # noqa: BLE001
            failed.append(asset)
            raise TileOutsideBounds(str(exc)) from exc

    try:
        img, _ = mosaic_reader(assets, one, x, y, z,
                               allowed_exceptions=(TileOutsideBounds,), **kwargs)
    except (TileOutsideBounds, EmptyMosaicError):
        img = None
    return img, failed


def read_dem_tile(dem, x, y, z, buffer=1, tilesize=256, resampling=None):
    """Lê o tile (z/x/y) do DEM em Web-Mercator com uma borda de `buffer` px pra
    declividade ter vizinhos nas beiradas. Retorna (height, mask) em float64 /
    bool com shape (tilesize+2*buffer,)*2, ou None se nada cobrir o tile.

    `resampling` (default RESAMPLING=bilinear): quem chama passa `average` quando
    a leitura DECIMA a fonte — bilinear pula pixels e serrilha."""
    resampling = resampling or RESAMPLING
    # Os DOIS DEMs são EPSG:4326 → o rio-tiler lê por um WarpedVRT (→ Web
    # Mercator) JÁ na resolução de saída: quem reamostra de verdade é o WARP
    # (`reproject_method`, default `nearest`!), não o `resampling_method` (esse
    # só age no read final, que aí já é 1:1). Com nearest, decimar 633→512 px
    # pula uma coluna a cada ~4 px e uma linha a cada ~7 → a declividade
    # (derivada) vira GRADE fina em todo tile. Os dois recebem o mesmo método.
    rkw = dict(resampling_method=resampling, reproject_method=resampling)
    try:
        if dem == "sp":
            with Reader(SAMPA_DEM_URL, options=SAMPA_READER_OPTS) as r:
                img = r.tile(x, y, z, tilesize=tilesize, buffer=buffer, **rkw)
        else:
            b = TMS.bounds(morecantile.Tile(x, y, z))
            # Guarda: mosaico 1°×1° não serve zoom muito afastado (abriria COGs
            # demais). Acima do span/contagem máximos → sem relevo (transparente).
            if (b.right - b.left) > MOSAIC_MAX_SPAN_DEG or \
               (b.top - b.bottom) > MOSAIC_MAX_SPAN_DEG:
                return None
            assets = _fabdem_assets_for_bounds(b.left, b.bottom, b.right, b.top)
            n = tilesize + 2 * buffer
            if not assets:              # só mar: 0 m, sem pedido nenhum
                return np.zeros((n, n)), np.ones((n, n), dtype=bool)
            if len(assets) > MOSAIC_MAX_ASSETS:
                return None
            img, failed = _mosaic_tile(assets, x, y, z, tilesize=tilesize,
                                       buffer=buffer, **rkw)
            if img is None:
                if failed:
                    raise DEMReadError(f"FABDEM {z}/{x}/{y}: COG falhou: {failed}")
                return np.zeros((n, n)), np.ones((n, n), dtype=bool)   # só mar
            band = img.array[0]
            m = ~np.ma.getmaskarray(band)
            # Buraco + COG que FALHOU = falha; buraco sem falha = mar (0 m).
            if not m.all() and failed:
                raise DEMReadError(f"FABDEM {z}/{x}/{y}: COG falhou: {failed}")
            h = np.where(m, np.ma.filled(band, 0.0).astype(np.float64), 0.0)
            return h, np.ones_like(m)   # mar dentro das células = 0 m
    except (TileOutsideBounds, EmptyMosaicError):
        return None
    except DEMReadError:
        raise
    except Exception:  # noqa: BLE001 — DEM-SP fora do ar etc.
        return None

    band = img.array[0]  # MaskedArray (H, W)
    height = np.ma.filled(band, np.nan).astype(np.float64)
    if band.mask is np.ma.nomask:
        mask = np.isfinite(height)
    else:
        mask = (~band.mask) & np.isfinite(height)
    if dem == "sp":
        mask &= height > SAMPA_MIN_VALID_M
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
    f = render_fields(dem, x, y, z, tilesize=tilesize, max_read=max_read)
    if f is None:
        return None
    height, mask, slope = f
    rgba = shade(height, mask, slope, elev_min, elev_max, slope_max, gamma, cycles)
    return _png_bytes(rgba)


def render_fields(dem, x, y, z, *, tilesize=256, max_read=None):
    """Os CAMPOS escalares do tile, já no tamanho do tile e ANTES da paleta:
    (elevação m, máscara bool, declividade m/m), ou None sem cobertura. Toda a
    parte cara (leitura nativa, declividade, reamostragem sem costura) mora
    aqui; `shade` é só cor — e é o que o navegador refaz sozinho a partir do
    /field/ (field_tile), sem voltar ao servidor quando os params mudam."""
    cap_px = max(MIN_READ_SIZE, min(SS_HARD_MAX, int(max_read or MAX_READ_SIZE)))
    tier = pick_tier(x, y, z, cap_px) if dem == "fabdem" else None
    if tier is not None:
        return _tier_fields(tier, x, y, z, tilesize)   # zoom afastado: tier
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

    return height, mask, slope


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
    # compress_level=1, não optimize=True: relevo é imagem "natural", o zlib
    # pesado quase não ganha (medido: 112 KiB em ambos) e custava ~65 ms de CPU
    # por tile contra ~9 ms — num Cloud Run de poucas vCPU isso é throughput.
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


# ── Campos pro navegador colorir (/field/) ───────────────────────────────────
# PNG RGB OPACO 256×512 (sem alfa: canvas pré-multiplica alfa e perderia bits):
#   linhas 0–255   elevação Terrarium, passo FIELD_ELEV_STEP_M:
#                  h = R·256 + G + B/256 − 32768
#   linhas 256–511 declividade em LOG (R·256+G = k; k=0 → s=0, senão
#                  s = FIELD_SLOPE_S0 · (1+FIELD_SLOPE_REL)^(k−1)) e B = máscara
#                  (255 coberto, 0 sem dado).
# Log = erro RELATIVO constante (2%): o slopeMax vai de 1% a 120% e o que importa
# na cor é s/slopeMax. Linear 16 bits dobrava o PNG; 8 bits bandava em terreno
# plano. Medido contra o shade() do servidor: ≤2 níveis (de 255) em settings
# normais; ~7 no p99.9 com faixa de 20 m em 8 ciclos. Piso S0 baixo porque γ
# alto (s_norm^(1/γ)) amplifica a declividade quase-nula. Estes números são
# CONTRATO com o decodificador do index.html (FIELD_* lá) — mudar = bumpar
# FIELD_VERSION nos dois.
FIELD_VERSION = "1"
FIELD_ELEV_STEP_M = 1.0 / 16.0
FIELD_SLOPE_S0 = 1e-6
FIELD_SLOPE_REL = 0.02


def field_tile(dem, x, y, z, tilesize=256, max_read=None):
    """PNG dos campos (ver acima) — independe de TODOS os params de cor."""
    f = render_fields(dem, x, y, z, tilesize=tilesize, max_read=max_read)
    if f is None:
        h = np.zeros((tilesize, tilesize)); s = np.zeros_like(h)
        m = np.zeros((tilesize, tilesize), dtype=bool)
    else:
        h, m, s = f
    h = np.where(np.isfinite(h), h, 0.0)
    v = np.round((np.clip(h, -11000.0, 9000.0) + 32768.0) / FIELD_ELEV_STEP_M) * FIELD_ELEV_STEP_M
    vi = np.floor(v)
    top = np.empty((tilesize, tilesize, 3), dtype=np.uint8)
    top[..., 0] = (vi // 256).astype(np.uint8)
    top[..., 1] = (vi % 256).astype(np.uint8)
    top[..., 2] = np.floor((v - vi) * 256.0).astype(np.uint8)
    s = np.where(np.isfinite(s), s, 0.0)
    k = np.where(s > FIELD_SLOPE_S0,
                 np.round(np.log(np.maximum(s, FIELD_SLOPE_S0) / FIELD_SLOPE_S0)
                          / math.log1p(FIELD_SLOPE_REL)) + 1, 0)
    k = np.clip(k, 0, 65535).astype(np.int64)
    bot = np.empty((tilesize, tilesize, 3), dtype=np.uint8)
    bot[..., 0] = (k >> 8).astype(np.uint8)
    bot[..., 1] = (k & 255).astype(np.uint8)
    bot[..., 2] = np.where(m, 255, 0).astype(np.uint8)
    buf = io.BytesIO()
    # nível 6 (não 1): o campo é cacheado longo e compartilhado — bytes pesam
    # mais que os ~60 ms de CPU, pagos uma vez por tile.
    Image.fromarray(np.concatenate([top, bot], 0), "RGB").save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


# ── Terreno 3D: elevação crua em Terrarium (raster-dem do MapLibre) ──────────
# Versão do ENCODING/leitura do terreno — chave de cache/ETag E o `v=` que a UI
# manda (TERRAIN_VERSION do index.html). Bumpe os DOIS juntos, como o par
# RENDER/TILE_VERSION (os tiles têm max-age de 7 dias).
TERRAIN_VERSION = "1"
# Zoom máximo NATIVO do terreno por fonte (acima o MapLibre sobreamplia): ~1 px
# de tile por célula nativa. FABDEM 30 m → z12 (~35 m/px em SP); DEM-SP 5 m → z15.
TERRAIN_MAXZOOM = {"fabdem": 12, "sp": 15}
# Quantização da elevação (m). O MapLibre não precisa de sub-decímetro pra
# malha; os bits baixos ruidosos só incham o PNG.
TERRAIN_STEP_M = 0.125


def terrain_tile(dem, x, y, z, tilesize=256):
    """PNG Terrarium (RGB: h = R·256 + G + B/256 − 32768) da elevação do DEM.

    NUNCA transparente: o MapLibre decodifica pixel (0,0,0) como −32768 m — um
    poço até o fundo do mundo. Sem dado vira 0 m (oceano no FABDEM). O DEM-SP
    só cobre a RMSP: fora dele (e nos buracos) completa com o FABDEM, senão a
    borda da cobertura viraria um penhasco até o nível do mar.

    Devolve None se NADA foi lido: oceano e falha de rede/R2 são indistinguíveis
    aqui (read_dem_tile engole os dois) e um tile de 0 m cacheado por 7 dias
    viraria um poço — quem chama serve terrain_flat_png() com cache curto."""
    b = TMS.bounds(morecantile.Tile(x, y, z))
    res = _mercator_res_m(z, (b.bottom + b.top) / 2.0)   # m/px do tile
    def read(src):
        tier = pick_tier(x, y, z, tilesize) if src == "fabdem" else None
        if tier is not None:                                  # zoom afastado: tier
            h, m, _ = _tier_fields(tier, x, y, z, tilesize)   # mar = 0 m; falha levanta
            return h, m
        native = SP_NATIVE_M if src == "sp" else FABDEM_NATIVE_M
        # decimando de verdade → média de ÁREA (bilinear pularia pixels)
        rs = "average" if res > native * 1.05 else RESAMPLING
        return read_dem_tile(src, x, y, z, buffer=0, tilesize=tilesize, resampling=rs)

    h = np.zeros((tilesize, tilesize), dtype=np.float64)
    have = np.zeros((tilesize, tilesize), dtype=bool)
    for src in (("sp", "fabdem") if dem == "sp" else ("fabdem",)):
        r = read(src)
        if r is not None:
            hh, mm = r
            fill = mm & ~have
            h[fill] = hh[fill]
            have |= fill
        if have.all():
            break
    if not have.any():
        return None
    return _terrarium_png(h)


def terrain_flat_png(tilesize=256):
    """Terreno plano a 0 m (sem dado / falha) — Terrarium, nunca transparente."""
    return _terrarium_png(np.zeros((tilesize, tilesize), dtype=np.float64))


def _terrarium_png(h):
    tilesize = h.shape[0]
    v = np.round((np.clip(h, -11000.0, 9000.0) + 32768.0) / TERRAIN_STEP_M) * TERRAIN_STEP_M
    vi = np.floor(v)
    rgb = np.empty((tilesize, tilesize, 3), dtype=np.uint8)
    rgb[..., 0] = (vi // 256).astype(np.uint8)
    rgb[..., 1] = (vi % 256).astype(np.uint8)
    rgb[..., 2] = np.floor((v - vi) * 256.0).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", compress_level=6)
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
            with Reader(SAMPA_DEM_URL, options=SAMPA_READER_OPTS) as r:
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
# Teto em BYTES, não em nº de entradas: os corpos variam 1 KB (transparente) a
# ~170 KB (campo 256×512), e o teto de 512 ENTRADAS chegou a ~80 MB e, somado
# aos renders simultâneos, estourou os 512 MiB do Cloud Run (OOM → instância
# morta no meio dos renders → tiles de 90 s + cold start).
PNG_CACHE_MAX_BYTES = int(os.environ.get("CAMERATOPO_CACHE_MB") or 64) * 1024 * 1024
_png_cache_bytes = 0


def cache_get(key):
    with _png_cache_lock:
        if key in _png_cache:
            _png_cache.move_to_end(key)
            return _png_cache[key]
    return None


def cache_put(key, value):
    global _png_cache_bytes
    with _png_cache_lock:
        old = _png_cache.pop(key, None)
        if old is not None:
            _png_cache_bytes -= len(old)
        _png_cache[key] = value
        _png_cache_bytes += len(value)
        while _png_cache_bytes > PNG_CACHE_MAX_BYTES and len(_png_cache) > 1:
            _, v = _png_cache.popitem(last=False)
            _png_cache_bytes -= len(v)


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
