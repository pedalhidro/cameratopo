# Câmera Topográfica — tile server XYZ. rasterio/rio-tiler trazem wheels
# manylinux com GDAL (+ PROJ/GEOS/curl/tiff…) EMBUTIDO, então não precisa do
# GDAL do sistema. MAS o wheel ainda linka `libexpat` — uma das libs que a
# política manylinux assume presente na base e que a `python:3.12-slim` NÃO
# traz. Sem ela, `import rasterio` quebra com
#   ImportError: libexpat.so.1: cannot open shared object file
# e o worker do gunicorn não sobe (Cloud Run responde 503 em todo tile). É a
# ÚNICA lib de sistema que falta (verificado: com libexpat1 o import e a
# leitura de COG via /vsicurl/ funcionam).
FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY render.py server.py ee_source.py ./
COPY web ./web

# Leitura eficiente de COG remoto via /vsicurl/.
ENV GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif \
    GDAL_HTTP_MULTIRANGE=YES \
    VSI_CACHE=TRUE \
    PORT=8080

# 1 worker (estado só é cache em memória; concorrência vem das threads, escala
# por instâncias — mesma razão do backend do amora).
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 60 server:app
