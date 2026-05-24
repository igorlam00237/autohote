"""Tests d'intégration : capture des emails de code de login Airbnb.

Vérifie que `/webhook/email` détecte correctement un email de login Airbnb
(via X-Template LIKE 'LOGIN_%' ou patterns subject), extrait le code à 6
chiffres et l'insère dans la table `airbnb_auth_codes`.
"""
import hashlib
import hmac
import json
import time

import pytest


MAILGUN_KEY = "test-mailgun-signing-key"


def _sign(token: str, timestamp: str, key: str = MAILGUN_KEY) -> str:
    return hmac.new(
        key.encode(), (timestamp + token).encode(), hashlib.sha256
    ).hexdigest()


def _login_headers_json(template: str = "LOGIN_VERIFICATION") -> str:
    return json.dumps([
        ["X-Category", "auth"],
        ["X-Template", template],
        ["X-Locale", "fr"],
    ])


def _post_login_email(client, *, body: str, subject: str = "",
                     headers_json: str | None = None):
    timestamp = str(int(time.time()))
    token = "x" * 50
    fields = {
        "signature": _sign(token, timestamp),
        "timestamp": timestamp,
        "token": token,
        "sender": "automated@airbnb.com",
        "From": "Airbnb <automated@airbnb.com>",
        "subject": subject or "Votre code de connexion Airbnb",
        "stripped-text": body,
        "Message-Id": f"<login-{timestamp}@airbnb.com>",
        "message-headers": headers_json or _login_headers_json(),
    }
    return client.post(
        "/webhook/email",
        data=fields,
        content_type="application/x-www-form-urlencoded",
    )


@pytest.fixture(autouse=True)
def truncate_auth_codes(db_conn):
    """Vide airbnb_auth_codes avant chaque test."""
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE airbnb_auth_codes RESTART IDENTITY;")
    db_conn.commit()


@pytest.fixture(autouse=True)
def configure_env(monkeypatch):
    monkeypatch.setenv("MAILGUN_SIGNING_KEY", MAILGUN_KEY)
    import app
    monkeypatch.setattr(app, "MAILGUN_SIGNING_KEY", MAILGUN_KEY)


@pytest.mark.integration
class TestLoginEmailCapture:
    def test_template_login_inserts_code(self, client, db_conn):
        resp = _post_login_email(
            client,
            body="Bonjour, votre code de connexion est 482931. Il expire dans 10 minutes.",
            headers_json=_login_headers_json("LOGIN_VERIFICATION"),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["type"] == "airbnb_login_code"

        with db_conn.cursor() as cur:
            cur.execute("SELECT code, raw_template FROM airbnb_auth_codes;")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "482931"
        assert rows[0][1] == "LOGIN_VERIFICATION"

    def test_template_auth_inserts_code(self, client, db_conn):
        resp = _post_login_email(
            client,
            body="Your verification code: 123456",
            headers_json=_login_headers_json("AUTH_EMAIL_CODE"),
        )
        assert resp.status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT code FROM airbnb_auth_codes;")
            assert cur.fetchone()[0] == "123456"

    def test_subject_fallback_no_template(self, client, db_conn):
        """Si X-Template ne matche pas mais le sujet contient 'code de connexion',
        on capte quand même."""
        resp = _post_login_email(
            client,
            body="Voici votre code : 999888",
            subject="Votre code de connexion Airbnb",
            headers_json=json.dumps([["X-Category", "auth"], ["X-Template", "OTHER"]]),
        )
        assert resp.status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT code FROM airbnb_auth_codes;")
            assert cur.fetchone()[0] == "999888"

    def test_login_email_without_extractable_code_ignored(self, client, db_conn):
        """Si l'email est détecté comme login mais sans code numérique trouvable."""
        resp = _post_login_email(
            client,
            body="Vous avez reçu une demande de connexion. Cliquez ici.",
            headers_json=_login_headers_json("LOGIN_VERIFICATION"),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ignored"
        assert body["reason"] == "login_email_without_code"
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM airbnb_auth_codes;")
            assert cur.fetchone()[0] == 0

    def test_login_capture_does_not_trigger_message_pipeline(self, client, db_conn, seed_logement):
        """Un email login ne doit PAS créer de message ou de conversation."""
        _post_login_email(
            client,
            body="Code : 555444",
            headers_json=_login_headers_json("LOGIN_VERIFICATION"),
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM messages;")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM conversations;")
            assert cur.fetchone()[0] == 0
