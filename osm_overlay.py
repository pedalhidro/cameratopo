"""Camada "Traçado OSM" — só vias, ferrovias e água, resto TRANSPARENTE.

A ideia é a do overlay "touring" do OsmAnd: as linhas do mapa (rodovias, ruas,
trilhas, ferrovias, rios) por cima de qualquer outra camada — relevo, satélite —
sem o fundo opaco do OSM cobrir o que está embaixo.

Como não existe um raster público "só linhas" do OSM sem chave de API, o
servidor proxeia o tile do openstreetmap-carto padrão e faz uma EXTRAÇÃO POR
COR, pixel a pixel: cada classe de via do carto tem cor de preenchimento/borda
fixa e o fundo é chapado (#f2efe9), então a classificação por distância RGB é
confiável. Pixels casados são RECOLORIDOS numa versão mais escura/saturada da
cor da classe (os originais são pálidos — sumiriam sobre o relevo colorido);
o resto vira alfa 0. Rótulos (texto) não casam com nenhuma cor-alvo e ficam de
fora — nomes de rua aparecem como "furos" na via, aceitável e até útil.

Sem costura por construção: a operação é estritamente por pixel (nenhuma
vizinhança), então tiles vizinhos nunca divergem na emenda.

Cores-alvo verificadas empiricamente contra tiles reais do carto (2026) — se o
estilo padrão do osm.org mudar a paleta, recalibrar aqui e bumpar a versão.

Política de tiles do OSMF: User-Agent identificando o app + cache de 7 dias no
servidor E no navegador (o proxy reduz a carga vs. o uso direto que a UI já
fazia do osm.org como base).
"""

from __future__ import annotations

import io
import urllib.request

import numpy as np
from PIL import Image

# Versão do overlay — vai na chave de cache/ETag do servidor e no `v=` da URL
# da UI (OSM_TRACADO_VERSION do index.html — bump JUNTO ao mudar cores/regras).
OSM_OVERLAY_VERSION = "1"

OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_MAX_ZOOM = 19          # o osm.org serve até z19; acima a UI reamplia
_TIMEOUT_S = 12
_UA = "cameratopo/1.0 (+https://cameratopo.pedalhidrografi.co)"

# (nome, cor-alvo no carto, tolerância RGB euclidiana, cor de saída).
# Primeira que casa vence. Tolerâncias apertadas onde o alvo é próximo de cores
# de fundo (branco #ffffff vs fundo #f2efe9 distam só ~30; prédio #d9d0c9 e
# calçada #dddddc rondam os cinzas) e folgadas nas saturadas, que não têm
# vizinho perigoso. Saída = mesma família de cor, mais escura/saturada, pra ler
# por cima da paleta cíclica do relevo.
_FEATURES = [
    # água (rios/represas/canais — preenchimento E linha de waterway)
    ("agua",       (170, 211, 223), 22, (36, 111, 170)),
    # rodovias (preenchimento + borda por classe)
    ("motorway",   (232, 146, 162), 20, (200, 30, 90)),
    ("motorway-c", (220, 42, 103),  24, (200, 30, 90)),
    ("trunk",      (249, 178, 156), 16, (205, 90, 45)),
    ("trunk-c",    (205, 108, 79),  18, (205, 90, 45)),
    ("primary",    (252, 214, 164), 14, (185, 130, 25)),
    ("primary-c",  (160, 107, 0),   20, (185, 130, 25)),
    ("secondary",  (247, 250, 191), 12, (125, 138, 20)),
    ("secondary-c",(112, 125, 5),   20, (125, 138, 20)),
    # vias menores: branco (terciária/residencial/serviço) + borda cinza
    ("minor",      (255, 255, 255), 12, (70, 76, 86)),
    ("minor-c",    (186, 185, 184), 10, (150, 156, 164)),
    ("pedestrian", (221, 221, 232), 10, (120, 126, 142)),
    # trilhas/ciclovias (pontilhados do carto) e ferrovia
    ("footway",    (250, 128, 114), 18, (210, 80, 55)),
    ("cycleway",   (28, 28, 246),   28, (40, 70, 210)),
    ("bridleway",  (0, 128, 0),     22, (0, 110, 0)),
    ("track",      (172, 131, 39),  20, (140, 100, 30)),
    ("rail",       (113, 113, 113), 14, (70, 70, 70)),
]


def _fetch_osm_png(z, x, y):
    req = urllib.request.Request(OSM_TILE_URL.format(z=z, x=x, y=y),
                                 headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
        return r.read()


def extract_lines(png_bytes):
    """PNG do carto → RGBA só com as linhas (recoloridas), resto alfa 0."""
    # int32: a distância² chega a 3·255² ≈ 195k — int16 estourava e o overflow
    # fazia QUALQUER cor "casar" (o overlay saía ~opaco inteiro).
    rgb = np.asarray(
        Image.open(io.BytesIO(png_bytes)).convert("RGB"), dtype=np.int32)
    h, w = rgb.shape[:2]
    out = np.zeros((h, w, 4), dtype=np.uint8)
    assigned = np.zeros((h, w), dtype=bool)
    for _name, target, tol, color in _FEATURES:
        d2 = ((rgb - np.array(target, dtype=np.int32)) ** 2).sum(axis=-1)
        m = (d2 <= tol * tol) & ~assigned
        if not m.any():
            continue
        out[m, 0], out[m, 1], out[m, 2], out[m, 3] = (*color, 255)
        assigned |= m
    return out


def render_overlay_tile(z, x, y):
    """Busca o tile OSM e devolve o PNG "só linhas". Exceção → o server serve
    transparente sem cachear (mesma convenção das camadas EE)."""
    rgba = extract_lines(_fetch_osm_png(z, x, y))
    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
