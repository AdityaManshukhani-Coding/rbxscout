# RbxScout dashboard — hosted container image.
#
# Runtime behavior (no secrets baked in):
#   * The app finds no rbx_scout.db in the image, so catalog_fetch.py runs in
#     hosted mode: it downloads the public catalog release asset into
#     RBXSCOUT_CATALOG_CACHE_DIR and re-checks it every 5 minutes.
#   * APP_PASSWORD      — the gate's master password (required, fail-closed).
#   * CONTACTS_KEY      — hex key from contacts.key; restores Discord links
#                         from the encrypted contacts.bundle after each
#                         catalog install (optional: without it the app works,
#                         links just stay hidden).
#   * SS_PROFILE_DIR    — per-device user profiles; mount a volume here so
#                         users stay remembered across container restarts.
#
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer only rebuilds when requirements change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + brand assets. The repo's rbx_scout.db is deliberately excluded
# via .dockerignore — the container must fetch the catalog like any user
# deployment, never ship a stale one.
COPY app.py gate.py profile_store.py catalog_fetch.py contacts_privacy.py \
     db_sync.py scout_core.py ./
COPY assets ./assets

# Catalog cache + user profiles live on the mounted volume.
RUN mkdir -p /data/catalog /data/profiles
ENV RBXSCOUT_CATALOG_CACHE_DIR=/data/catalog \
    SS_PROFILE_DIR=/data/profiles

EXPOSE 8501

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=8)" || exit 1

CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
