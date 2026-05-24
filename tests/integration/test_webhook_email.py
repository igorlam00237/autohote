"""Tests d'intégration de l'endpoint /webhook/email (Mailgun).

Couvre :
- Vérification HMAC-SHA256 (signature correcte / incorrecte / absente)
- Filtre sender (rejette les emails non-Airbnb)
- Body vide
- Extraction du nom du voyageur depuis From / Subject
- Extraction du thread_id depuis In-Reply-To / References / Message-Id
- Pipeline complet jusqu'au brouillon Claude (mocké)
- Idempotence via Message-Id
"""
import hashlib
import hmac
import json
import os
import pathlib
import time

import pytest

MAILGUN_KEY = "test-mailgun-signing-key"
FIXTURES_DIR = pathlib.Path("/app/tests/fixtures/airbnb_emails")


def _sign(token: str, timestamp: str, key: str = MAILGUN_KEY) -> str:
    return hmac.new(
        key=key.encode(),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _airbnb_headers_json(category="message",
                         template="MESSAGING_NEW_MESSAGE_EMAIL_DIGEST",
                         extra=None):
    """Construit le champ message-headers JSON-encoded façon Mailgun.

    Par défaut on simule un VRAI email-message Airbnb (X-Category=message
    + X-Template MESSAGING_*). Les tests qui veulent simuler un autre type
    (rappel, paiement…) doivent passer override category/template.
    """
    headers = [
        ["X-Category", category],
        ["X-Template", template],
        ["X-Locale", "fr"],
    ]
    for k, v in (extra or {}).items():
        headers.append([k, v])
    return json.dumps(headers)


def _mailgun_post(client, *, fields: dict, key: str = MAILGUN_KEY,
                   bad_signature: bool = False):
    """Forge un POST Mailgun signé. Si bad_signature=True, casse volontairement
    la signature pour tester la sécurité.

    Injecte par défaut des `message-headers` simulant un VRAI message Airbnb
    (X-Category=message). Les tests peuvent surcharger via la clé spéciale
    `_message_headers_json` dans fields, ou désactiver le filtre.
    """
    timestamp = str(int(time.time()))
    token = "x" * 50
    sig = _sign(token, timestamp, key=key)
    if bad_signature:
        sig = "0" * 64

    # Permet aux tests de surcharger les headers (catégorie, template) :
    headers_json = fields.pop("_message_headers_json", None) or _airbnb_headers_json()

    payload = {
        "signature": sig,
        "timestamp": timestamp,
        "token": token,
        "message-headers": headers_json,
        **fields,
    }
    return client.post(
        "/webhook/email",
        data=payload,
        content_type="application/x-www-form-urlencoded",
    )


@pytest.fixture(autouse=True)
def configure_mailgun_env(monkeypatch):
    monkeypatch.setenv("MAILGUN_SIGNING_KEY", MAILGUN_KEY)
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-fake")
    import app
    monkeypatch.setattr(app, "MAILGUN_SIGNING_KEY", MAILGUN_KEY)
    monkeypatch.setattr(app, "CLAUDE_API_KEY", "sk-ant-fake")

    def fake_call_claude(system_blocks, user_message):
        return {
            "id": "msg_fake",
            "content": [{"type": "text", "text": f"Mock reply: {user_message[:30]}"}],
            "stop_reason": "end_turn",
            "usage": {},
        }
    monkeypatch.setattr(app, "call_claude", fake_call_claude)


@pytest.mark.integration
class TestMailgunSignature:
    def test_invalid_signature_returns_401(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Marie sent you a message",
            "stripped-text": "Bonjour",
            "Message-Id": "<test-1@airbnb.com>",
        }, bad_signature=True)
        assert resp.status_code == 401

    def test_missing_signature_returns_401(self, client, seed_logement):
        resp = client.post("/webhook/email", data={
            "sender": "automated@airbnb.com",
            "stripped-text": "Bonjour",
        })
        assert resp.status_code == 401

    def test_valid_signature_accepted(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Marie sent you a message",
            "stripped-text": "Bonjour, à quelle heure puis-je arriver ?",
            "Message-Id": "<msg-001@airbnb.com>",
        })
        assert resp.status_code == 200


@pytest.mark.integration
class TestMailgunSenderFilter:
    def test_non_airbnb_sender_ignored(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "spam@evil.com",
            "From": "Spammer <spam@evil.com>",
            "subject": "Buy crypto",
            "stripped-text": "Click here",
            "Message-Id": "<spam-001@evil.com>",
        })
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ignored"
        assert resp.get_json()["reason"] == "not_from_airbnb"

    def test_airbnb_fr_accepted(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.fr",
            "From": "Marie <noreply@airbnb.fr>",
            "subject": "Nouveau message",
            "stripped-text": "Bonjour",
            "Message-Id": "<msg-fr-001@airbnb.fr>",
        })
        assert resp.status_code == 200
        assert resp.get_json()["status"] in ("ok", "partial")

    def test_empty_body_ignored(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Marie sent you a message",
            "stripped-text": "",
            "body-plain": "",
            "Message-Id": "<empty-001@airbnb.com>",
        })
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ignored"
        assert resp.get_json()["reason"] == "empty_body"


@pytest.mark.integration
class TestVoyageurNameExtraction:
    def test_extracts_from_header_name(self, client, seed_logement, db_conn):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie Dupont via Airbnb <noreply@airbnb.com>",
            "subject": "Marie Dupont vous a envoyé un message",
            "stripped-text": "Bonjour",
            "Message-Id": "<name-001@airbnb.com>",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["parsed"]["voyageur_nom"] == "Marie Dupont"

    def test_filters_via_airbnb_suffix(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "John Smith via Airbnb <noreply@airbnb.com>",
            "subject": "John Smith sent you a message",
            "stripped-text": "Hi",
            "Message-Id": "<name-002@airbnb.com>",
        })
        body = resp.get_json()
        assert body["parsed"]["voyageur_nom"] == "John Smith"

    def test_falls_back_to_subject(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Airbnb <automated@airbnb.com>",  # pas de nom
            "subject": "Sophie Martin sent you a message about Studio",
            "stripped-text": "Hello",
            "Message-Id": "<name-003@airbnb.com>",
        })
        body = resp.get_json()
        assert body["parsed"]["voyageur_nom"] == "Sophie Martin"

    def test_falls_back_to_default(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Airbnb <automated@airbnb.com>",
            "subject": "Tu as un nouveau message",  # pas de nom identifiable
            "stripped-text": "Hello",
            "Message-Id": "<name-004@airbnb.com>",
        })
        body = resp.get_json()
        assert body["parsed"]["voyageur_nom"] == "Voyageur"


@pytest.mark.integration
class TestThreadIdExtraction:
    def test_references_root_takes_priority(self, client, seed_logement):
        """Sur un email avancé dans un thread, le 1er id de References (root)
        doit être utilisé, PAS l'In-Reply-To (qui pointe vers le prédécesseur
        immédiat seulement)."""
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Re: ...",
            "stripped-text": "3e message du thread",
            "Message-Id": "<msg-3@airbnb.com>",
            "In-Reply-To": "<msg-2@airbnb.com>",  # prédécesseur immédiat
            "References": "<thread-root@airbnb.com> <msg-1@airbnb.com> <msg-2@airbnb.com>",
        })
        body = resp.get_json()
        # On veut le root, pas msg-2
        assert body["parsed"]["thread_id_externe"] == "<thread-root@airbnb.com>"

    def test_in_reply_to_used_when_no_references(self, client, seed_logement):
        """Cas du 1er reply : certains clients ne fournissent que In-Reply-To,
        pas de References. On accepte In-Reply-To en fallback."""
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Re: ...",
            "stripped-text": "1er reply",
            "Message-Id": "<msg-2@airbnb.com>",
            "In-Reply-To": "<msg-1@airbnb.com>",
            # Pas de References
        })
        body = resp.get_json()
        assert body["parsed"]["thread_id_externe"] == "<msg-1@airbnb.com>"

    def test_uses_message_id_if_no_threading(self, client, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Nouveau message",
            "stripped-text": "Premier message",
            "Message-Id": "<new-thread@airbnb.com>",
        })
        body = resp.get_json()
        assert body["parsed"]["thread_id_externe"] == "<new-thread@airbnb.com>"

    def test_long_thread_stays_in_same_conversation(self, client, db_conn, seed_logement):
        """Test de non-régression du bug : 4 emails successifs du même thread
        doivent atterrir dans la MÊME conversation BDD, pas en créer 4 différentes."""
        root_id = "<thread-marathon@airbnb.com>"

        # Email 1 : initial
        r1 = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Marie sent you a message",
            "stripped-text": "Question 1",
            "Message-Id": root_id,
        })
        conv_1 = r1.get_json()["conversation_id"]

        # Email 2 : Marie répond — References contient le root
        r2 = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Re: ...",
            "stripped-text": "Question 2 (suite)",
            "Message-Id": "<msg-2@airbnb.com>",
            "In-Reply-To": root_id,
            "References": root_id,
        })
        conv_2 = r2.get_json()["conversation_id"]

        # Email 3 : Marie re-répond — In-Reply-To pointe vers msg-2, MAIS
        # References garde le root → le bug aurait créé une nouvelle conv ici
        r3 = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Re: ...",
            "stripped-text": "Question 3",
            "Message-Id": "<msg-3@airbnb.com>",
            "In-Reply-To": "<msg-2@airbnb.com>",
            "References": f"{root_id} <msg-2@airbnb.com>",
        })
        conv_3 = r3.get_json()["conversation_id"]

        # Email 4 : encore plus loin
        r4 = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Re: ...",
            "stripped-text": "Question 4",
            "Message-Id": "<msg-4@airbnb.com>",
            "In-Reply-To": "<msg-3@airbnb.com>",
            "References": f"{root_id} <msg-2@airbnb.com> <msg-3@airbnb.com>",
        })
        conv_4 = r4.get_json()["conversation_id"]

        # ASSERTION CRITIQUE : tous les 4 doivent partager la même conv
        assert conv_1 == conv_2 == conv_3 == conv_4, \
            f"Threading cassé : conversations {conv_1}, {conv_2}, {conv_3}, {conv_4}"

        # Et on doit avoir 4 messages voyageur + 4 brouillons dans cette conv
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM messages WHERE conversation_id = %s AND role = 'user';",
                (conv_1,)
            )
            assert cur.fetchone()[0] == 4
            cur.execute(
                "SELECT count(*) FROM messages WHERE conversation_id = %s AND role = 'assistant';",
                (conv_1,)
            )
            assert cur.fetchone()[0] == 4


@pytest.mark.integration
class TestAirbnbCategoryFilter:
    """Tests des filtres header-based introduits en Phase 1."""

    def test_non_message_category_is_ignored(self, client, seed_logement):
        """Un rappel Airbnb (X-Category=reminder) doit être ignoré."""
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Airbnb <automated@airbnb.com>",
            "subject": "Rappel : plus que 5 heures pour répondre",
            "stripped-text": "Bonjour Armel, n'oubliez pas...",
            "Message-Id": "<reminder-001@airbnb.com>",
            "_message_headers_json": _airbnb_headers_json(
                category="reminder",
                template="REMINDER_HOST_RESPONSE",
            ),
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ignored"
        assert body["reason"] == "not_a_message_email"
        assert body["category"] == "reminder"

    def test_non_messaging_template_is_ignored(self, client, seed_logement):
        """X-Category=message mais template non-MESSAGING_ → ignoré."""
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Airbnb <automated@airbnb.com>",
            "subject": "Confirmation de paiement",
            "stripped-text": "Votre paiement a été reçu",
            "Message-Id": "<pay-001@airbnb.com>",
            "_message_headers_json": _airbnb_headers_json(
                category="message",
                template="PAYMENT_RECEIVED",
            ),
        })
        body = resp.get_json()
        assert body["status"] == "ignored"
        assert body["reason"] == "not_a_message_email"

    def test_missing_headers_is_ignored(self, client, seed_logement):
        """Pas de message-headers → on ne peut pas vérifier la catégorie, ignoré."""
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Airbnb <automated@airbnb.com>",
            "subject": "Test sans headers",
            "stripped-text": "blabla",
            "Message-Id": "<noheaders-001@airbnb.com>",
            "_message_headers_json": "[]",
        })
        body = resp.get_json()
        assert body["status"] == "ignored"

    def test_booking_initial_inquiry_passes_filter(self, client, seed_logement):
        """Régression observée le 24 mai 2026 : Airbnb envoie un email de
        type 'BOOKING_INITIAL_INQUIRY' avec X-Category=support pour une
        première demande d'info voyageur sur une annonce. Notre filtre
        original (qui n'acceptait que MESSAGING_*) rejetait à tort cet
        email pourtant légitime. Le whitelist élargi doit le traiter."""
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Airbnb <automated@airbnb.com>",
            "subject": "Chambre moderne 14 places : demande d'information",
            "stripped-text": (
                "MARIE\nResponsable de la réservation\n\n"
                "Bonjour, votre logement est-il dispo ?\n"
                "https://www.airbnb.fr/hosting/thread/9988776655"
            ),
            "Message-Id": "<inquiry-001@airbnb.com>",
            "_message_headers_json": _airbnb_headers_json(
                category="support",
                template="BOOKING_INITIAL_INQUIRY",
            ),
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] in ("ok", "partial")
        assert body["parsed"]["thread_id_externe"] == "9988776655"


@pytest.mark.integration
class TestAirbnbBodyExtraction:
    """Extraction depuis le body Airbnb (thread_id, listing_id, voyageur)."""

    def _post_with_fixture(self, client):
        body = (FIXTURES_DIR / "demande_information_body.txt").read_text(encoding="utf-8")
        return _mailgun_post(client, fields={
            "sender": "express@airbnb.com",
            "From": "Airbnb <express@airbnb.com>",
            "subject": "Demande d'information pour Chambre moderne, 23-24 mai",
            "stripped-text": body,
            "Message-Id": "<fixture-msg-001@airbnb.com>",
            "Reply-To": "abc123token@reply.airbnb.com",
        })

    def test_real_email_fixture_thread_id(self, client, seed_logement):
        resp = self._post_with_fixture(client)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] in ("ok", "partial")
        assert body["parsed"]["thread_id_externe"] == "9999988888"

    def test_real_email_fixture_listing_id(self, client, seed_logement):
        resp = self._post_with_fixture(client)
        body = resp.get_json()
        assert body["parsed"]["airbnb_listing_id"] == "1111122222333344445"

    def test_real_email_fixture_voyageur_name(self, client, seed_logement):
        resp = self._post_with_fixture(client)
        body = resp.get_json()
        # Le bloc body contient "MARIE DUPONT" en UPPERCASE — on re-capitalize
        assert body["parsed"]["voyageur_nom"] == "Marie Dupont"

    def test_listing_id_persisted_on_conversation(self, client, db_conn, seed_logement):
        resp = self._post_with_fixture(client)
        conv_id = resp.get_json()["conversation_id"]
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT airbnb_listing_id FROM conversations WHERE id = %s;",
                (conv_id,),
            )
            assert cur.fetchone()[0] == "1111122222333344445"


@pytest.mark.integration
class TestMailgunHappyPath:
    def test_creates_full_pipeline(self, client, db_conn, seed_logement):
        resp = _mailgun_post(client, fields={
            "sender": "automated@airbnb.com",
            "From": "Marie Dupont via Airbnb <noreply@airbnb.com>",
            "subject": "Marie Dupont sent you a message about Test Logement",
            "stripped-text": "Bonjour, quelle est l'adresse exacte du logement ?",
            "Message-Id": "<happy-001@airbnb.com>",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["draft_id"] is not None
        assert body["parsed"]["voyageur_nom"] == "Marie Dupont"

        # Vérifie l'état BDD
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT role, contenu_propose, statut FROM messages WHERE id = %s;",
                (body["draft_id"],)
            )
            role, propose, statut = cur.fetchone()
            assert role == "assistant"
            assert "Mock reply" in propose
            assert statut == "pending"

    def test_idempotency_via_message_id(self, client, db_conn, seed_logement):
        fields = {
            "sender": "automated@airbnb.com",
            "From": "Marie <noreply@airbnb.com>",
            "subject": "Marie sent a message",
            "stripped-text": "Bonjour",
            "Message-Id": "<idem-001@airbnb.com>",
        }
        r1 = _mailgun_post(client, fields=fields)
        r2 = _mailgun_post(client, fields=fields)
        assert r1.get_json()["draft_id"] == r2.get_json()["draft_id"]
        assert r2.get_json()["already_processed"] is True

        # Un seul message stocké
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM messages WHERE external_message_id = %s;",
                ("<idem-001@airbnb.com>",)
            )
            assert cur.fetchone()[0] == 1
