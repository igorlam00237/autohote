-- Migration 001 — Ajout external_message_id pour idempotence des webhooks
-- ----------------------------------------------------------------------
-- Permet à un webhook (Apify ou autre) de renvoyer plusieurs fois le même
-- message sans créer de doublons en BDD.
--
-- Date : 2026-05-23 — Semaine 4
-- Idempotent : peut être relancée sans casser l'existant.

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS external_message_id TEXT;

-- Index unique partiel : empêche les doublons quand external_message_id est renseigné,
-- mais autorise NULL multiple (pour les messages internes générés par Claude).
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_message_id
    ON messages (external_message_id)
    WHERE external_message_id IS NOT NULL;
