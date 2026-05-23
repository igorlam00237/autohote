-- Migration 003 — File d'attente de jobs pour le scraper Playwright
-- ----------------------------------------------------------------------
-- Table persistante de jobs asynchrones. Le webhook Flask et le dashboard
-- enqueue des jobs ; le futur worker scraper (Phase 3) les claim de façon
-- atomique via `FOR UPDATE SKIP LOCKED` et les exécute.
--
-- Types de jobs (job_type) :
--   - process_inbound        : sync conversation + génération brouillon Claude
--   - send_reply             : envoi d'une réponse validée via Airbnb
--   - login                  : refresh de session Airbnb (auth email-code)
--   - daily_reconciliation   : scrape quotidien pour combler les gaps
--
-- Statuts (status) :
--   - pending     : en attente de claim
--   - running     : en cours d'exécution par un worker
--   - completed   : terminé avec succès
--   - failed      : échec final après retry max
--   - cancelled   : annulé manuellement
--
-- Date : 2026-05-23 — Semaine 4 Phase 2
-- Idempotent : peut être relancée sans casser l'existant.

CREATE TABLE IF NOT EXISTS scraper_jobs (
    id              BIGSERIAL PRIMARY KEY,
    job_type        TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    scheduled_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    last_error      TEXT,
    conversation_id INTEGER REFERENCES conversations(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Index partiel : seuls les jobs en attente sont scannés par les workers.
-- Ordre (scheduled_at, id) → FIFO strict avec stabilité sur le hash collision.
CREATE INDEX IF NOT EXISTS idx_scraper_jobs_pending
    ON scraper_jobs (scheduled_at, id)
    WHERE status = 'pending';

-- Index pour le debounce / dédoublonnage par conversation.
-- Permet la requête "y a-t-il un job récent de ce type pour cette conv ?"
CREATE INDEX IF NOT EXISTS idx_scraper_jobs_conversation
    ON scraper_jobs (conversation_id, job_type, created_at DESC)
    WHERE conversation_id IS NOT NULL;
