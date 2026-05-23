"""Tests d'intégration de l'endpoint /webhook/reception.

Couvre :
- Auth par token X-Webhook-Secret
- Validation du payload (logement_id, message_text)
- Pipeline complet : insertion user msg + appel Claude (mocké) + insertion draft
- Idempotence via external_message_id
- Gestion d'erreurs (logement inconnu, Claude API down)
"""
import json
import os

import pytest


def _post_webhook(client, payload, headers=None, secret=None):
    base_headers = {
        "Content-Type": "application/json",
        "X-Webhook-Secret": secret or os.environ.get("WEBHOOK_SECRET", "test-webhook-secret"),
    }
    if headers:
        base_headers.update(headers)
    return client.post(
        "/webhook/reception",
        data=json.dumps(payload),
        headers=base_headers,
    )


@pytest.fixture(autouse=True)
def configure_webhook_env(monkeypatch):
    """Définit un WEBHOOK_SECRET stable et patche call_claude pour éviter le réseau."""
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-fake")

    # Patche les constantes du module app à la valeur de l'env (l'import a déjà eu lieu)
    import app
    monkeypatch.setattr(app, "WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setattr(app, "CLAUDE_API_KEY", "sk-ant-fake")

    # Stub call_claude pour ne PAS faire d'appel réseau pendant les tests
    def fake_call_claude(system_blocks, user_message):
        return {
            "id": "msg_fake_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": f"Réponse mockée à : {user_message[:40]}"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }
    monkeypatch.setattr(app, "call_claude", fake_call_claude)


@pytest.mark.integration
class TestWebhookAuth:
    def test_no_secret_returns_401(self, client, seed_logement):
        resp = client.post(
            "/webhook/reception",
            data=json.dumps({"logement_id": seed_logement, "message_text": "Hi"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401

    def test_wrong_secret_returns_401(self, client, seed_logement):
        resp = _post_webhook(
            client,
            {"logement_id": seed_logement, "message_text": "Hi"},
            secret="mauvais-token",
        )
        assert resp.status_code == 401

    def test_correct_secret_accepted(self, client, seed_logement):
        resp = _post_webhook(
            client, {"logement_id": seed_logement, "message_text": "Hi"}
        )
        assert resp.status_code == 200


@pytest.mark.integration
class TestWebhookValidation:
    def test_missing_logement_id_returns_400(self, client):
        resp = _post_webhook(client, {"message_text": "Hi"})
        assert resp.status_code == 400

    def test_missing_message_text_returns_400(self, client, seed_logement):
        resp = _post_webhook(client, {"logement_id": seed_logement})
        assert resp.status_code == 400

    def test_empty_message_text_returns_400(self, client, seed_logement):
        resp = _post_webhook(client, {"logement_id": seed_logement, "message_text": "   "})
        assert resp.status_code == 400

    def test_unknown_logement_returns_404(self, client):
        resp = _post_webhook(client, {"logement_id": 99999, "message_text": "Hi"})
        assert resp.status_code == 404


@pytest.mark.integration
class TestWebhookHappyPath:
    def test_creates_conversation_and_messages(self, client, db_conn, seed_logement):
        resp = _post_webhook(client, {
            "logement_id": seed_logement,
            "thread_id_externe": "thread-abc-001",
            "voyageur_nom": "Marie Dupont",
            "voyageur_langue": "fr",
            "message_text": "Bonjour, quelle est l'adresse du logement ?",
            "external_message_id": "msg-ext-001",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["already_processed"] is False
        assert body["conversation_id"] is not None
        assert body["user_message_id"] is not None
        assert body["draft_id"] is not None

        # Vérifie l'état BDD
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT voyageur_nom, voyageur_langue, thread_id_externe "
                "FROM conversations WHERE id = %s;", (body["conversation_id"],)
            )
            v_nom, v_lang, t_id = cur.fetchone()
            assert v_nom == "Marie Dupont"
            assert v_lang == "fr"
            assert t_id == "thread-abc-001"

            cur.execute(
                "SELECT role, contenu_original, statut, external_message_id "
                "FROM messages WHERE id = %s;", (body["user_message_id"],)
            )
            role, contenu, statut, ext_id = cur.fetchone()
            assert role == "user"
            assert "adresse" in contenu
            assert statut == "received"
            assert ext_id == "msg-ext-001"

            cur.execute(
                "SELECT role, contenu_propose, statut FROM messages WHERE id = %s;",
                (body["draft_id"],)
            )
            role, propose, statut = cur.fetchone()
            assert role == "assistant"
            assert "Réponse mockée" in propose
            assert statut == "pending"

    def test_reuses_existing_conversation_for_same_thread(self, client, db_conn, seed_logement):
        # 1er message
        resp1 = _post_webhook(client, {
            "logement_id": seed_logement,
            "thread_id_externe": "thread-shared",
            "voyageur_nom": "John",
            "message_text": "Message 1",
            "external_message_id": "ext-1",
        })
        # 2e message dans le même thread
        resp2 = _post_webhook(client, {
            "logement_id": seed_logement,
            "thread_id_externe": "thread-shared",
            "voyageur_nom": "John",
            "message_text": "Message 2",
            "external_message_id": "ext-2",
        })
        assert resp1.get_json()["conversation_id"] == resp2.get_json()["conversation_id"]

    def test_history_appears_in_dashboard_pending(self, client, auth_header, seed_logement):
        _post_webhook(client, {
            "logement_id": seed_logement,
            "thread_id_externe": "thread-display",
            "voyageur_nom": "Marie",
            "message_text": "Quel est le code WiFi ?",
            "external_message_id": "ext-display-1",
        })
        # Le dashboard doit voir le brouillon pending
        dash = client.get("/", headers=auth_header)
        assert dash.status_code == 200
        assert b"Marie" in dash.data
        assert b"WiFi" in dash.data
        assert b"R\xc3\xa9ponse mock\xc3\xa9e" in dash.data  # "Réponse mockée" UTF-8


@pytest.mark.integration
class TestWebhookIdempotency:
    def test_same_external_id_returns_cached_result(self, client, db_conn, seed_logement):
        payload = {
            "logement_id": seed_logement,
            "thread_id_externe": "thread-idem",
            "voyageur_nom": "Marie",
            "message_text": "Premier appel",
            "external_message_id": "ext-idem-001",
        }
        # 1er appel
        r1 = _post_webhook(client, payload)
        assert r1.status_code == 200
        b1 = r1.get_json()
        assert b1["already_processed"] is False

        # 2e appel identique (Apify renvoie le même message) → ne doit PAS créer de doublon
        r2 = _post_webhook(client, payload)
        assert r2.status_code == 200
        b2 = r2.get_json()
        assert b2["already_processed"] is True
        assert b2["conversation_id"] == b1["conversation_id"]
        assert b2["user_message_id"] == b1["user_message_id"]

        # Vérifie qu'il n'y a qu'un seul message côté BDD pour ce external_id
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM messages WHERE external_message_id = %s;",
                (payload["external_message_id"],)
            )
            assert cur.fetchone()[0] == 1


@pytest.mark.integration
class TestWebhookClaudeFailure:
    def test_claude_failure_keeps_user_message_returns_502(self, client, db_conn, seed_logement, monkeypatch):
        import app
        import urllib.error
        from io import BytesIO

        # Force call_claude à lever une HTTPError simulée
        def failing_call(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="https://api.anthropic.com/v1/messages",
                code=500, msg="Server Error",
                hdrs=None, fp=BytesIO(b'{"error":"upstream"}'),
            )
        monkeypatch.setattr(app, "call_claude", failing_call)

        resp = _post_webhook(client, {
            "logement_id": seed_logement,
            "message_text": "Test sans Claude",
            "external_message_id": "ext-noclaude-1",
        })
        assert resp.status_code == 502
        body = resp.get_json()
        assert body["status"] == "partial"
        assert body["user_message_id"] is not None
        assert body["draft_id"] is None

        # Le message voyageur DOIT être en BDD (on a un journal complet)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT role, statut FROM messages WHERE id = %s;",
                (body["user_message_id"],)
            )
            role, statut = cur.fetchone()
            assert role == "user"
            assert statut == "received"
