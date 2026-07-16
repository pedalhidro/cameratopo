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
roles/earthengine.viewer + serviceusage.serviceUsageConsumer.

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
# RENDER_VERSION para esta fonte). Bumpe ao mudar a expressão EE.
EE_VERSION = "1"

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
    req = urllib.request.Request(url, headers={"User-Agent": "cameratopo/ee"})
    with urllib.request.urlopen(req, timeout=_TILE_TIMEOUT_S) as r:
        return r.read()
