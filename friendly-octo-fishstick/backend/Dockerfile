# Svarah API (FastAPI) — production image, used by Render (see /render.yaml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# FFmpeg encodes the reel and campaign video, Tesseract backs the Guardian's
# poster OCR check, and the fonts cover the poster compositor (DejaVu) and the
# Indic scripts in frames rendered by headless Chromium (Noto).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# The app resolves paths from the folder above `backend/`, so keep that shape.
WORKDIR /srv/backend

COPY requirements.txt ./
RUN pip install -r requirements.txt "playwright>=1.45" \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

# Render injects PORT; 10000 is its default.
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
