-- Migration 004 — Stockage temporaire des codes de connexion Airbnb
-- ----------------------------------------------------------------------
-- Quand le scraper déclenche un login Airbnb, Airbnb envoie un code à 6
-- chiffres par email. Cet email est capté par Mailgun (X-Template LIKE
-- 'LOGIN_%') puis le code est inséré ici, où le scraper le récupère.
--
-- TTL court : les codes Airbnb expirent en ~10 min. On garde 1h pour debug
-- puis on les nettoie (peut être fait par une requête manuelle au début).
--
-- Date : 2026-05-24 — Semaine 4 Phase 3a
-- Idempotent : peut être relancée sans casser l'existant.

CREATE TABLE IF NOT EXISTS airbnb_auth_codes (
    id         BIGSERIAL PRIMARY KEY,
    code       TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    consumed_at TIMESTAMPTZ,
    raw_subject TEXT,
    raw_template TEXT
);

-- Index pour récupérer rapidement le code non consommé le plus récent
CREATE INDEX IF NOT EXISTS idx_airbnb_auth_codes_unconsumed
    ON airbnb_auth_codes (received_at DESC)
    WHERE consumed_at IS NULL;
