# ---------- build ----------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TT_HOST=0.0.0.0 \
    TT_PORT=8000 \
    TT_DOWNLOAD_DIR=/downloads \
    TT_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg aria2 nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 10001 tube \
    && mkdir -p /downloads /data \
    && chown -R tube:tube /app /downloads /data

USER tube

EXPOSE 8000
VOLUME ["/downloads", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["python", "-m", "app", "serve", "--host", "0.0.0.0", "--port", "8000"]
