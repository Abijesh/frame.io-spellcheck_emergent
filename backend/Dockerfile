# FrameCheck backend.
#
# CPU-only by default (portable, no GPU host required): EasyOCR's gpu=True in
# ocr_service.py auto-falls-back to CPU when no CUDA device is present, so
# this image runs correctly anywhere -- just slower (~73x per the cost model
# in the project's cost-analysis doc). To switch to GPU once GPU hosting is
# actually budgeted:
#   1. Base this on a CUDA runtime image (e.g. nvidia/cuda:12.1-runtime).
#   2. After `pip install -r requirements.txt`, add:
#      RUN pip install --no-cache-dir --force-reinstall torch torchvision \
#          --index-url https://download.pytorch.org/whl/cu121
#   3. Deploy to a host with an actual GPU + nvidia-container-toolkit.

FROM python:3.12-slim

# ffmpeg for frame extraction. Playwright's own --with-deps below installs
# the Chromium runtime libraries, so they aren't listed here by hand.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Separate layer so `docker build` only reinstalls deps when requirements.txt
# actually changes, not on every source edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN python -m playwright install --with-deps chromium

COPY . .

# Run as non-root; the app already launches Chromium with --no-sandbox
# (see services/frameio_service.py), which is what makes that safe here.
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /home/appuser/.EasyOCR /home/appuser/.cache/ms-playwright \
    && cp -r /root/.cache/ms-playwright/* /home/appuser/.cache/ms-playwright/ \
    && chown -R appuser:appuser /home/appuser
USER appuser
ENV HOME=/home/appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -f "http://localhost:${PORT:-8000}/api/config" || exit 1

# MONGO_URL, DB_NAME, GEMINI_API_KEY, CORS_ORIGINS are read from the
# environment at runtime (see backend/.env locally) -- set them as secrets/env
# vars on whichever host runs this image, never bake them into the image.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
