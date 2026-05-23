-- Migration 002 — Ajout airbnb_listing_id à conversations
-- ----------------------------------------------------------------------
-- Stocke l'ID de l'annonce Airbnb (URL /rooms/<id>) lié à la conversation.
-- Utile pour matcher la conversation au bon logement quand on en aura
-- plusieurs, et pour relier la conversation à l'annonce dans le dashboard.
--
-- Date : 2026-05-23 — Semaine 4 Phase 1
-- Idempotent : peut être relancée sans casser l'existant.

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS airbnb_listing_id TEXT;

CREATE INDEX IF NOT EXISTS idx_conversations_airbnb_listing_id
    ON conversations (airbnb_listing_id)
    WHERE airbnb_listing_id IS NOT NULL;
