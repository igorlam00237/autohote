# Image officielle Playwright avec Chromium et toutes les deps système préinstallées.
# Plus simple que de tout installer manuellement depuis python:slim — économise ~30 min
# de troubleshooting de dépendances graphiques.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Installe libpq pour psycopg2 (l'image Playwright n'a pas postgres deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Code source du scraper et du module jobs partagé.
# /app/dashboard est monté en read-only depuis docker-compose.
COPY . /app/scraper/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/dashboard:/app/scraper

# Volume monté à /app/state pour persister la session Playwright entre runs
ENV SCRAPER_STATE_DIR=/app/state \
    PLAYWRIGHT_HEADLESS=true

# Par défaut on lance le worker. Override possible via docker compose run
# pour des commandes ponctuelles (ex: python -m scraper.airbnb_auth login)
CMD ["python", "-m", "scraper.worker"]
