-- =====================================================
-- AutoHôte — Schéma initial de la base de données
-- Phase MVP — PostgreSQL
-- =====================================================
-- Idempotent : peut être exécuté plusieurs fois sans casser
-- l'existant (CREATE IF NOT EXISTS, INSERT conditionnel).

-- -----------------------------------------------------
-- Table sources
-- Couche d'abstraction multi-plateforme.
-- Jamais hardcoder "airbnb" dans la logique métier.
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,            -- airbnb | booking | vrbo | direct
    config_json TEXT,
    actif       INTEGER DEFAULT 1,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- -----------------------------------------------------
-- Table logements
-- Fiche structurée injectée dans le prompt Claude.
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS logements (
    id              SERIAL PRIMARY KEY,
    source_id       INTEGER REFERENCES sources(id),
    nom             TEXT NOT NULL,
    adresse         TEXT,
    description     TEXT,
    regles          TEXT,
    equipements     TEXT,
    wifi_nom        TEXT,
    wifi_mdp        TEXT,
    code_acces      TEXT,
    heure_checkin   TEXT,
    heure_checkout  TEXT,
    contact_urgence TEXT,
    config_json     TEXT,
    actif           INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- -----------------------------------------------------
-- Table conversations
-- Fil d'échanges avec un voyageur.
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id                  SERIAL PRIMARY KEY,
    logement_id         INTEGER REFERENCES logements(id),
    voyageur_nom        TEXT,
    voyageur_langue     TEXT,                 -- détectée automatiquement
    thread_id_externe   TEXT,                 -- conversation_id côté Airbnb (extraite de l'URL email)
    airbnb_listing_id   TEXT,                 -- ID de l'annonce Airbnb (URL /rooms/<id>)
    statut              TEXT DEFAULT 'active',
    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversations_airbnb_listing_id
    ON conversations (airbnb_listing_id)
    WHERE airbnb_listing_id IS NOT NULL;

-- -----------------------------------------------------
-- Table messages
-- Chaque message entrant et sortant + brouillon Claude.
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                  SERIAL PRIMARY KEY,
    conversation_id     INTEGER REFERENCES conversations(id),
    role                TEXT NOT NULL,        -- user | assistant
    contenu_original    TEXT,                 -- message brut reçu / réponse envoyée
    contenu_propose     TEXT,                 -- brouillon généré par Claude
    contenu_envoye      TEXT,                 -- version finale après validation
    valide_par          TEXT,
    valide_at           TIMESTAMPTZ,
    statut              TEXT DEFAULT 'pending', -- pending | validated | sent | error | received | rejected
    external_message_id TEXT,                 -- id du message côté source externe (Apify/Airbnb) → idempotence webhook
    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- Index unique partiel pour empêcher les doublons sur les messages externes
-- tout en autorisant NULL multiple (messages internes générés par Claude)
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_message_id
    ON messages (external_message_id)
    WHERE external_message_id IS NOT NULL;

-- -----------------------------------------------------
-- Table scraper_jobs
-- File d'attente de jobs asynchrones consommés par le worker scraper.
-- Types : process_inbound, send_reply, login, daily_reconciliation.
-- Statuts : pending, running, completed, failed, cancelled.
-- -----------------------------------------------------
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

CREATE INDEX IF NOT EXISTS idx_scraper_jobs_pending
    ON scraper_jobs (scheduled_at, id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_scraper_jobs_conversation
    ON scraper_jobs (conversation_id, job_type, created_at DESC)
    WHERE conversation_id IS NOT NULL;

-- -----------------------------------------------------
-- Index pour les requêtes fréquentes
-- -----------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_logements_source       ON logements(source_id);
CREATE INDEX IF NOT EXISTS idx_conversations_logement ON conversations(logement_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation  ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_statut        ON messages(statut);

-- -----------------------------------------------------
-- Source par défaut : Airbnb (idempotent)
-- -----------------------------------------------------
INSERT INTO sources (name, type)
SELECT 'Airbnb', 'airbnb'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'Airbnb');
