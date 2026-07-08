# Câmera Topográfica — tile server XYZ. rasterio traz wheels manylinux com GDAL
# embutido, então a imagem slim basta (sem GDAL do sistema).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY render.py server.py ./

# Leitura eficiente de COG remoto via /vsicurl/.
ENV GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif \
    GDAL_HTTP_MULTIRANGE=YES \
    VSI_CACHE=TRUE \
    PORT=8080

# 1 worker (estado só é cache em memória; concorrência vem das threads, escala
# por instâncias — mesma razão do backend do amora).
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 60 server:app
