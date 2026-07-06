# syntax=docker/dockerfile:1
# python:*-slim ist multi-arch (amd64 + arm64) → läuft auf Intel- UND ARM-Synology.
# httpx ist pure Python — kein Compiler nötig.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivilegierter Runtime-User (UID 1000 = üblicher NAS-Erstnutzer).
RUN groupadd -g 1000 app && useradd -m -u 1000 -g app app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY ingest.py .

USER app
CMD ["python", "ingest.py"]
