# ════════════════════════════════════════════════════════════════════════════
# AeroTAX Backend — Multi-Stage Container Image
# ════════════════════════════════════════════════════════════════════════════
# Tauglich für Google Cloud Run (Phase B Migration):
#   - gunicorn als Production-Server (kein flask-dev-Server)
#   - bindet auf $PORT (Cloud Run injected, default 8080)
#   - workers=1 (Spec: concurrency=1 pro Container, weniger gleichzeitige RAM-Pressure)
#   - timeout=1800 (30 Min für lange CAS+Klassifikations-Jobs)
#   - PYTHONUNBUFFERED=1 → stdout/stderr direkt ans Logging
# Kompatibel mit Render (Procfile wird ignoriert wenn Dockerfile vorhanden).
# ════════════════════════════════════════════════════════════════════════════

FROM python:3.12.0-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Minimale Build-Dependencies für native Wheel-Builds (pillow, pillow-heif)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv .venv
COPY requirements.txt ./
RUN .venv/bin/pip install -r requirements.txt


# ─── Runtime-Image ─────────────────────────────────────────────────────────
FROM python:3.12.0-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# libheif-Runtime für pillow-heif (iPhone-Bilder bei optionalen Belegen)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libheif1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv .venv/
COPY . .

# Cloud Run schickt SIGTERM bei scale-down → gunicorn graceful-shutdown.
# BUG-005 Fix (2026-05-12):
#   worker-class=gthread + threads=8 → 8 concurrent requests pro Container.
#   Vorher: workers=1 threads=2 mit default sync-worker → bei Cloud Run
#   concurrency=10 staute sich die Gunicorn-Queue auf, ein hängender Supabase-
#   Call (z.B. /api/session mit großem result_data) blockierte Health/Forum.
#   Cloud-Run-Service muss `containerConcurrency=8` matchen (Tests:
#   tests/test_concurrency_invariants.py).
# timeout=1800s (30 Min) reicht für lange Worker-Jobs (process-job via Cloud Tasks).
# max-requests=200/jitter=20 für graceful restart vor Memory-Leak-Akkumulation.
CMD exec gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --worker-class gthread \
    --threads 8 \
    --timeout 1800 \
    --graceful-timeout 60 \
    --max-requests 200 \
    --max-requests-jitter 20 \
    --access-logfile - \
    --error-logfile -
