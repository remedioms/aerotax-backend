# AeroX EU headless-browser scraper — separate Cloud Run service.
# Base image ships Chromium + all system deps for Playwright (no apt gymnastics).
#
# BUILD CONTEXT = this eu_scraper/ directory (deploy with `--source ./eu_scraper`),
# so Cloud Build only uploads these few files — not the whole backend repo.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HEADLESS=1

WORKDIR /app

# Deps first (layer cache). The base already has the browsers for playwright 1.48;
# `playwright install chromium` is a cheap re-verify.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && playwright install chromium

# The code is imported as the `eu_scraper` package → copy it under that path.
COPY . /app/eu_scraper

EXPOSE 8080
# 1 worker, a few threads: a scrape holds one Chromium; an instance handles one
# scrape at a time. Long timeout — a full sweep can take minutes.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 4 \
    --timeout 600 eu_scraper.main:app
