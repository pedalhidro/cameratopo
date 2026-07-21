"""Fonte Google Earth Engine (`dem=ee`): tiles do FABDEM renderizados pelo EE.

Em vez de ler COGs e renderizar localmente (render.py), esta fonte constrói a
MESMA composição (elevação em cmocean.phase cíclica × realce de declividade
branco→preto γ-corrigido) como uma expressão Earth Engine, pede um mapid via
`getMapId()` e faz proxy dos PNGs dos tile servers do EE. É a referência viva
do `ee-cameratopo.js` rodando de verdade — útil como ground truth do porte
local e para declividade "de graça" em qualquer zoom.

Autenticação: Application Default Credentials — NENHUM segredo no repo (o
serviço roda no Cloud Run com `--service-account`; localmente, `gcloud auth
application-default login`). O projeto (CAMERATOPO_EE_PROJECT) precisa estar
registrado no Earth Engine e a service account precisa de
roles/earthengine.writer + serviceusage.serviceUsageConsumer (o getMapId
exige `earthengine.maps.create`, que só o writer tem — o viewer não basta).

Por que PROXY e não redirect: as URLs de mapid EXPIRAM (~4 h). Proxiando, a
expiração vira problema do servidor (refresh sob lock) e os tiles ganham o
mesmo Cache-Control de 7 dias + ETag versionado + mesma origem dos demais.

Sem costura por construção: os parâmetros de normalização moram no mapid — a
grade inteira compartilha a mesma expressão, então não há o problema de
tiles normalizando diferente (invariante nº 1 do CLAUDE.md).

Unidades: `slope_max` chega em m/m (tangente), como no render local. O
`ee.Terrain.slope` devolve GRAUS → convertemos com tan(rad(slope)) antes de
normalizar, para a querystring significar o mesmo nas três fontes.
"""

from __future__ import annotations

import math
import os
import threading
import time
import urllib.request
from collections import OrderedDict

import render  # CMO_PHASE — mesma paleta, mesmíssimos bytes

# Versão da FONTE EE — entra na chave de cache/ETag do tile (papel do
# RENDER_VERSION para esta fonte). Bumpe ao mudar a expressão EE da composição.
EE_VERSION = "1"

# Versão do REGISTRY de camadas (endpoint /ee/<camada>/…): chave de cache/ETag
# dos tiles das camadas E o `v=` que a UI manda. Bumpe ao mudar qualquer
# expressão de camada (independente da composição acima).
EE_LAYERS_VERSION = "2"

# Projeto GCP registrado no Earth Engine em cujo nome os getMapId são feitos.
EE_PROJECT = os.environ.get("CAMERATOPO_EE_PROJECT") or "pedal-hidrografico"

# Endpoint high-volume: é o recomendado pelo Google para servir tiles
# programaticamente (mais QPS, sem cache de sessão interativa).
EE_API_URL = "https://earthengine-highvolume.googleapis.com"

# mapids expiram em ~4 h; renovamos com folga. Cache pequeno e LRU: os params
# são quantizados na chave (ver _param_key), mas o endpoint é público — sem
# teto, uma querystring fuzzada cresceria o dict sem limite.
MAPID_TTL_S = int(os.environ.get("CAMERATOPO_EE_MAPID_TTL") or 3 * 3600)
MAPID_CACHE_MAX = int(os.environ.get("CAMERATOPO_EE_MAPID_MAX") or 64)

FABDEM_ASSET = "projects/sat-io/open-datasets/FABDEM"

_TILE_TIMEOUT_S = 20
# Camadas com reduceNeighborhood (worldpop, desmorro) estouram 20 s no primeiro
# tile (depois o EE cacheia e fica rápido) — timeout próprio, mais folgado.
_LAYER_TILE_TIMEOUT_S = 45

_lock = threading.Lock()
_initialized = False
_mapids: OrderedDict[str, tuple[str, float]] = OrderedDict()  # key → (url_format, deadline)

# Paleta em hex, derivada do MESMO array do render local (17 âncoras cíclicas).
CMO_PHASE_HEX = ["%02x%02x%02x" % tuple(int(v) for v in c) for c in render.CMO_PHASE]


def _ensure_init():
    """ee.Initialize() preguiçoso e idempotente — não bloqueia o boot do worker
    (lição do gunicorn/libexpat: manter o boot enxuto e falhar por request)."""
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        import ee
        import google.auth

        creds, _ = google.auth.default(scopes=[
            "https://www.googleapis.com/auth/earthengine",
            "https://www.googleapis.com/auth/cloud-platform",
        ])
        ee.Initialize(credentials=creds, project=EE_PROJECT, opt_url=EE_API_URL)
        _initialized = True


def _build_image(elev_min, elev_max, slope_max, gamma, cycles):
    """Expressão EE equivalente ao shade() do render.py (e ao ee-cameratopo.js):
    elevação visualizada na paleta cíclica × (1 − (tan(declive)/slopeMax)^(1/γ)).
    """
    import ee

    col = ee.ImageCollection(FABDEM_ASSET)
    proj = col.first().select(0).projection()
    # Como no ee-cameratopo.js: mosaico na projeção nativa (a declividade TEM
    # que ser derivada no grid nativo — invariante nº 2), depois bicubic.
    elev = col.mosaic().setDefaultProjection(proj)
    slope_deg = ee.Terrain.slope(elev)
    elev_s = elev.resample("bicubic")
    slope_s = slope_deg.resample("bicubic")

    # graus → m/m (tangente): mesma unidade de slopeMax nas outras fontes.
    slope_tan = slope_s.multiply(math.pi / 180.0).tan()

    # Paleta repetida N vezes = N ciclos na faixa (a paleta é cíclica: última
    # âncora == primeira, então a emenda entre ciclos é contínua).
    palette = CMO_PHASE_HEX * max(1, int(cycles))
    ele_rgb = elev_s.visualize(min=elev_min, max=elev_max, palette=palette)

    inv_gamma = 1.0 / max(0.05, gamma or 1.2)
    s_norm = slope_tan.divide(max(1e-9, slope_max)).clamp(0.0, 1.0).pow(inv_gamma)
    factor = ee.Image.constant(1.0).subtract(s_norm)

    # 3 bandas × 1 banda: o EE aplica a banda única a todas. Fora da cobertura
    # do FABDEM (oceano) a declividade é mascarada → tile transparente, como no
    # render local (sem unmask(0) de propósito).
    return ele_rgb.multiply(factor).toUint8()


def _param_key(p):
    """Chave quantizada do mapid — mesma precisão do ETag do server.py, para o
    jitter de slider não cunhar mapids novos (e queimar quota) à toa."""
    return (f"v{EE_VERSION}?e={p['elev_min']:.3f},{p['elev_max']:.3f}"
            f"&s={p['slope_max']:.6f}&g={p['gamma']:.3f}&c={p['cycles']}")


def _mapid_for(p):
    """URL-template do tile ({z}/{x}/{y}) para os params, com cache TTL + LRU.
    Double-checked lock (mesmo padrão do _auto_lock do render): getMapId é uma
    chamada de rede — sem o lock, um burst de tiles frios faria N chamadas."""
    key = _param_key(p)
    now = time.monotonic()

    ent = _mapids.get(key)
    if ent and ent[1] > now:
        return ent[0]

    with _lock:
        ent = _mapids.get(key)
        if ent and ent[1] > now:
            return ent[0]

        img = _build_image(p["elev_min"], p["elev_max"], p["slope_max"],
                           p["gamma"], p["cycles"])
        map_id = img.getMapId({"min": 0, "max": 255})
        url_format = map_id["tile_fetcher"].url_format

        _mapids[key] = (url_format, now + MAPID_TTL_S)
        _mapids.move_to_end(key)
        while len(_mapids) > MAPID_CACHE_MAX:
            _mapids.popitem(last=False)
        return url_format


def fetch_tile(z, x, y, p):
    """PNG do tile via EE (bytes), ou levanta — o server.py já trata qualquer
    exceção como tile transparente (invariante: nunca derrubar o tile server)."""
    _ensure_init()
    url = _mapid_for(p).format(z=z, x=x, y=y)
    return _fetch_png(url)


def _fetch_png(url, timeout=_TILE_TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": "cameratopo/ee"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ═══ Registry de camadas do app GEE (endpoint /ee/<camada>/{z}/{x}/{y}.png) ═══
#
# As MESMAS camadas do app Câmera Topográfica no Earth Engine (o
# `camera-topografica` do Code Editor é a referência canônica), cada uma como
# uma expressão EE que devolve uma imagem JÁ VISUALIZADA (RGB) — getMapId({})
# uniforme, proxy igual ao da composição. Camadas "de contexto" (MapBiomas,
# luzes, aridez…) têm mapid fixo; as derivadas do relevo (elevação, declives)
# entram na chave com os params quantizados da querystring (mesma quantização
# do ETag — jitter de slider não cunha mapid).
#
# A FeatureView dos rios não existe fora do Code Editor — a camada `rios`
# pinta a FeatureCollection subjacente (mesma paleta/propriedade), que é o
# equivalente servível.
#
# Assets PRIVADOS (claro3g, eleições — projects/ee-danilolessa/assets/*): só
# renderizam se a identidade do ADC tiver leitura no asset (localmente o dono;
# no Cloud Run a SA precisa ser adicionada como reader no Code Editor). Sem
# acesso → tile transparente, como qualquer falha de EE (por construção).

# Paletas inline (o Code Editor resolve via require(); aqui é literal):
# gena/ee-palettes (gee-community) — cmocean.Curl[7] e misc.parula[7].
CURL7 = ["151d44", "156c72", "7eb390", "fdf5f4", "db8d77", "9c3060", "340d35"]
PARULA7 = ["352a87", "056ede", "089bce", "33b7a0", "a3bd6a", "f9bd3f", "f9fb0e"]

# MapBiomas Coleção 9 — cores oficiais por id de classe (Legenda-Colecao-9-
# LEGEND-CODE.pdf, brasil.mapbiomas.org). Vira lista de 70 entradas (ids 0–69,
# vis min=0 max=69), classes ausentes em branco (não ocorrem nos dados).
_MB9_CLASS_COLORS = {
    1: "1f8d49", 3: "1f8d49", 4: "7dc975", 5: "04381d", 6: "026975",
    49: "02d659", 10: "ad975a", 11: "519799", 12: "d6bc74", 32: "fc8114",
    29: "ffaa5f", 50: "ad5100", 14: "ffffb2", 15: "edde8e", 18: "e974ed",
    19: "c27ba0", 39: "f5b3c8", 20: "db7093", 40: "c71585", 62: "ff69b4",
    41: "f54ca9", 36: "d082de", 46: "d68fe2", 47: "9932cc", 35: "9065d0",
    48: "e6ccff", 9: "7a5900", 21: "ffefc3", 22: "d4271e", 23: "ffa07a",
    24: "d4271e", 30: "9c0027", 25: "db4d4f", 26: "0000ff", 33: "2532e4",
    31: "091077", 27: "ffffff",
}
MAPBIOMAS9 = [_MB9_CLASS_COLORS.get(i, "ffffff") for i in range(70)]

# SLD do índice de aridez — strings idênticas às do app GEE.
_ARIDITY_ENTRIES = (
    '<ColorMapEntry color="#ff0000" quantity="0.03" label="0-0.03"/>'
    '<ColorMapEntry color="#ff8c00" quantity="0.21" label="0.03-0.2" />'
    '<ColorMapEntry color="#f2ff00" quantity="0.51" label="0.2-0.51" />'
    '<ColorMapEntry color="#dbfc03" quantity="0.65" label="0.5-0.65" />'
    '<ColorMapEntry color="#00ffa6" quantity="1.00" label="0.66-1.00" />'
    '<ColorMapEntry color="#00f2ff" quantity="1.50" label="1.00-1.50" />'
    '<ColorMapEntry color="#0084ff" quantity="2.5" label=">1.50" />'
)
ARIDITY_SLD_DISC = ('<RasterSymbolizer><ColorMap type="intervals">'
                    + _ARIDITY_ENTRIES + '</ColorMap></RasterSymbolizer>')
ARIDITY_SLD_CONT = ('<RasterSymbolizer><ColorMap>'
                    + _ARIDITY_ENTRIES + '</ColorMap></RasterSymbolizer>')

MAPBIOMAS_ASSET = ("projects/mapbiomas-public/assets/brazil/lulc/collection9/"
                   "mapbiomas_collection90_integration_v1")

# Como no app (boot): kernel de 1 km e ±2 desvios para a PTL.
PTL_KERNEL_M = 1000
PTL_MAX_SD = 2.0


def _fabdem_native():
    """Mosaico FABDEM na projeção NATIVA (invariante nº 2: declividade e
    vizinhanças derivam do grid nativo) — base comum das camadas de relevo."""
    import ee

    col = ee.ImageCollection(FABDEM_ASSET)
    proj = col.first().select(0).projection()
    return col.mosaic().setDefaultProjection(proj)


def _ptl(kernel_m=PTL_KERNEL_M):
    """Posição Topográfica Local: (média_vizinhança − elev)/desvio_vizinhança.
    unmask(0) como no app GEE (lá o elevationReprojected é unmask(0))."""
    import ee

    elev = _fabdem_native().unmask(0)
    kw = dict(kernel=ee.Kernel.circle(kernel_m, "meters", True), skipMasked=False)
    mean = elev.reduceNeighborhood(reducer=ee.Reducer.mean(), **kw)
    std = elev.reduceNeighborhood(reducer=ee.Reducer.stdDev(), **kw)
    return mean.subtract(elev).divide(std).resample("bilinear")


def _slope_norm(p):
    """Declividade nativa em m/m, normalizada por slopeMax com γ (mesma
    matemática do fator da composição)."""
    import ee

    slope_deg = ee.Terrain.slope(_fabdem_native()).resample("bicubic")
    slope_tan = slope_deg.multiply(math.pi / 180.0).tan()
    inv_gamma = 1.0 / max(0.05, p["gamma"] or 1.0)
    return (slope_tan.divide(max(1e-9, p["slope_max"]))
            .clamp(0.0, 1.0).pow(inv_gamma))


def _ly_aridez_disc(p):
    import ee
    img = ee.Image("projects/sat-io/open-datasets/global_ai/global_ai_yearly")
    return img.multiply(0.0001).sldStyle(ARIDITY_SLD_DISC)


def _ly_aridez_cont(p):
    import ee
    img = ee.Image("projects/sat-io/open-datasets/global_ai/global_ai_yearly")
    return img.multiply(0.0001).sldStyle(ARIDITY_SLD_CONT)


def _mapbiomas(year):
    import ee
    img = ee.Image(MAPBIOMAS_ASSET).select(f"classification_{year}")
    return img.visualize(min=0, max=69, palette=MAPBIOMAS9)


def _ly_luzes(p):
    import ee
    col = (ee.ImageCollection("NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG")
           .filter(ee.Filter.date("2020-09-01", "2024-09-01"))
           .select("avg_rad"))
    return col.mosaic().visualize(min=0.0, max=100.0, gamma=3.0)


def _ly_claro3g(p):
    import ee
    return ee.Image("projects/ee-danilolessa/assets/claro-3g").visualize()


def _ly_eleicoes_cart(p):
    import ee
    img = ee.Image("projects/ee-danilolessa/assets/2022-2oturno-cartograma")
    return img.unmask(0).visualize()


def _ly_eleicoes_terr(p):
    import ee
    img = ee.Image("projects/ee-danilolessa/assets/2022-2oturno-territorio")
    return img.visualize()


def _ly_worldpop(p):
    import ee
    col = (ee.ImageCollection("projects/sat-io/open-datasets/WORLDPOP/pop")
           .filter(ee.Filter.stringContains("system:index", "_POP_2025_")))
    img = (col.select([0], ["population"]).mosaic().unmask(0)
           .reduceNeighborhood(
               reducer=ee.Reducer.mean(),
               kernel=ee.Kernel.gaussian(3000, 1000, "meters", True),
               skipMasked=False))
    return img.visualize(min=0, max=200, gamma=2.0)


def _ly_desmorro(p):
    import ee
    ptl_std = _ptl().reduceNeighborhood(
        reducer=ee.Reducer.stdDev(),
        kernel=ee.Kernel.circle(3 * PTL_KERNEL_M, "meters", True),
        skipMasked=False).resample("bilinear")
    return ptl_std.visualize(min=0.5, max=1.0, gamma=0.15)


def _ly_elevacao(p):
    palette = CMO_PHASE_HEX * max(1, int(p["cycles"]))
    return (_fabdem_native().resample("bicubic")
            .visualize(min=p["elev_min"], max=p["elev_max"], palette=palette))


def _ly_declive(p):
    # branco no escuro: 0 = preto, slopeMax = branco (γ aplicado)
    return _slope_norm(p).visualize(min=0.0, max=1.0)


def _ly_declive_inv(p):
    # escuro no branco: inverte a normalização ANTES do γ, como no app
    # (out = ((max − s)/max)^(1/γ))
    import ee
    norm = _slope_norm(dict(p, gamma=1.0))          # sem γ: norm pura 0..1
    inv_gamma = 1.0 / max(0.05, p["gamma"] or 1.0)
    return (ee.Image.constant(1.0).subtract(norm)
            .pow(inv_gamma).visualize(min=0.0, max=1.0))


def _ly_ptl(p):
    """PTL parametrizada como no app GEE: ±N desvios saturam a paleta e o
    círculo da vizinhança tem `ptl_kernel` metros (sliders do painel ☰)."""
    sd = p.get("ptl_sd", PTL_MAX_SD)
    return _ptl(p.get("ptl_kernel", PTL_KERNEL_M)).visualize(
        min=-sd, max=sd, palette=CURL7)


def _ly_rios(p):
    import ee
    fc = ee.FeatureCollection("WWF/HydroSHEDS/v1/FreeFlowingRivers")
    img = ee.Image().byte().paint(fc, "RIV_ORD", 2)
    return img.visualize(min=1, max=9, palette=PARULA7)


# id → (builder, params de que a expressão depende: "" | "elev" | "slope").
# Ordem daqui = ordem de empilhamento padrão da UI (fundo → topo, como no app).
LAYERS = {
    "aridez-disc": (_ly_aridez_disc, ""),
    "aridez-cont": (_ly_aridez_cont, ""),
    "mapbiomas-2023": (lambda p: _mapbiomas(2023), ""),
    "mapbiomas-1985": (lambda p: _mapbiomas(1985), ""),
    "luzes": (_ly_luzes, ""),
    "claro3g": (_ly_claro3g, ""),
    "eleicoes-cart": (_ly_eleicoes_cart, ""),
    "eleicoes-terr": (_ly_eleicoes_terr, ""),
    "worldpop": (_ly_worldpop, ""),
    "desmorro": (_ly_desmorro, ""),
    "elevacao": (_ly_elevacao, "elev"),
    "declive": (_ly_declive, "slope"),
    "declive-inv": (_ly_declive_inv, "slope"),
    "ptl": (_ly_ptl, "ptl"),
    "rios": (_ly_rios, ""),
}


def layer_param_sig(layer, p):
    """Parte da chave (mapid E ETag) que depende dos params — só o que a camada
    realmente usa, para slider de elevação não invalidar camada estática."""
    dep = LAYERS[layer][1]
    if dep == "elev":
        return f"e={p['elev_min']:.3f},{p['elev_max']:.3f}&c={p['cycles']}"
    if dep == "slope":
        return f"s={p['slope_max']:.6f}&g={p['gamma']:.3f}"
    if dep == "ptl":
        return f"psd={p['ptl_sd']:.2f}&pk={p['ptl_kernel']:.0f}"
    return ""


def fetch_layer_tile(layer, z, x, y, p):
    """PNG de uma camada do registry (bytes), ou levanta (→ transparente)."""
    _ensure_init()
    key = f"ly/{layer}/v{EE_LAYERS_VERSION}?{layer_param_sig(layer, p)}"
    now = time.monotonic()

    ent = _mapids.get(key)
    if not ent or ent[1] <= now:
        with _lock:
            ent = _mapids.get(key)
            if not ent or ent[1] <= now:
                img = LAYERS[layer][0](p)
                map_id = img.getMapId({})
                ent = (map_id["tile_fetcher"].url_format, now + MAPID_TTL_S)
                _mapids[key] = ent
                _mapids.move_to_end(key)
                while len(_mapids) > MAPID_CACHE_MAX:
                    _mapids.popitem(last=False)

    return _fetch_png(ent[0].format(z=z, x=x, y=y), timeout=_LAYER_TILE_TIMEOUT_S)
