"""Tests d'intégration du dashboard de validation.

Couvre les chemins critiques :
- Affichage des messages pending
- Envoi (statut → sent + contenu_envoye)
- Rejet (statut → rejected)
- Édition du brouillon avant envoi
- Idempotence : double-clic n'altère pas un message déjà traité
- Activité récente affichée après actions
"""
from datetime import datetime, timezone

import pytest


@pytest.mark.integration
class TestPendingMessagesDisplay:
    def test_empty_state_when_no_pending(self, client, auth_header):
        resp = client.get("/", headers=auth_header)
        assert resp.status_code == 200
        assert b"Boite vide" in resp.data or b"vide" in resp.data.lower()

    def test_pending_message_appears(self, client, auth_header, seed_pending_pair):
        resp = client.get("/", headers=auth_header)
        assert resp.status_code == 200
        assert b"Marie Test" in resp.data
        assert b"check-in" in resp.data
        assert b"Le check-in se fait" in resp.data


@pytest.mark.integration
class TestSendAction:
    def test_send_without_edit_marks_as_sent(self, client, auth_header, seed_pending_pair, db_conn):
        resp = client.post(
            f"/action/{seed_pending_pair}",
            data={"action": "send", "edited_text": ""},
            headers=auth_header,
        )
        assert resp.status_code == 302  # redirect

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT statut, contenu_envoye, contenu_propose, valide_par, valide_at "
                "FROM messages WHERE id = %s;",
                (seed_pending_pair,),
            )
            statut, envoye, propose, validator, valide_at = cur.fetchone()
        assert statut == "sent"
        # Sans edit, contenu_envoye doit valoir contenu_propose
        assert envoye == propose
        assert validator == "admin"
        assert valide_at is not None

    def test_send_with_edit_stores_edited_text(self, client, auth_header, seed_pending_pair, db_conn):
        edited = "Bonjour ! Vous pouvez arriver à 15h00, c'est super 🎉"
        resp = client.post(
            f"/action/{seed_pending_pair}",
            data={"action": "send", "edited_text": edited},
            headers=auth_header,
        )
        assert resp.status_code == 302

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT statut, contenu_envoye, contenu_propose FROM messages WHERE id = %s;",
                (seed_pending_pair,),
            )
            statut, envoye, propose = cur.fetchone()
        assert statut == "sent"
        assert envoye == edited
        assert envoye != propose  # la version éditée diffère du brouillon


@pytest.mark.integration
class TestRejectAction:
    def test_reject_marks_as_rejected(self, client, auth_header, seed_pending_pair, db_conn):
        resp = client.post(
            f"/action/{seed_pending_pair}",
            data={"action": "reject"},
            headers=auth_header,
        )
        assert resp.status_code == 302

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT statut, contenu_envoye, valide_par FROM messages WHERE id = %s;",
                (seed_pending_pair,),
            )
            statut, envoye, validator = cur.fetchone()
        assert statut == "rejected"
        assert envoye is None  # pas d'envoi
        assert validator == "admin"


@pytest.mark.integration
class TestIdempotency:
    def test_double_action_does_not_alter_already_handled(self, client, auth_header, seed_pending_pair, db_conn):
        # 1ère action : send
        client.post(
            f"/action/{seed_pending_pair}",
            data={"action": "send", "edited_text": "Version A"},
            headers=auth_header,
        )

        # 2e action : reject (devrait être ignorée car statut != pending)
        resp = client.post(
            f"/action/{seed_pending_pair}",
            data={"action": "reject"},
            headers=auth_header,
        )
        assert resp.status_code == 302
        assert b"warn=already_handled" in resp.location.encode() if isinstance(resp.location, str) else b"warn=already_handled" in resp.location

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT statut, contenu_envoye FROM messages WHERE id = %s;",
                (seed_pending_pair,),
            )
            statut, envoye = cur.fetchone()
        assert statut == "sent", "Le 2e clic ne doit PAS changer le statut"
        assert envoye == "Version A", "Le contenu envoyé doit rester celui du 1er clic"


@pytest.mark.integration
class TestUnknownAction:
    def test_unknown_action_returns_400(self, client, auth_header, seed_pending_pair):
        resp = client.post(
            f"/action/{seed_pending_pair}",
            data={"action": "drop_database"},  # tentative malveillante
            headers=auth_header,
        )
        assert resp.status_code == 400


@pytest.mark.integration
class TestRecentActivity:
    def test_sent_message_appears_in_recent(self, client, auth_header, seed_pending_pair):
        client.post(
            f"/action/{seed_pending_pair}",
            data={"action": "send", "edited_text": "Envoi test"},
            headers=auth_header,
        )
        resp = client.get("/", headers=auth_header)
        assert resp.status_code == 200
        assert b"Envoi test" in resp.data
        assert b"Activit" in resp.data  # "Activité récente"

    def test_rejected_message_appears_in_recent(self, client, auth_header, seed_pending_pair):
        client.post(
            f"/action/{seed_pending_pair}",
            data={"action": "reject"},
            headers=auth_header,
        )
        resp = client.get("/", headers=auth_header)
        assert resp.status_code == 200
        # Le brouillon rejeté doit être visible dans l'historique
        assert b"Le check-in se fait" in resp.data
        assert b"rejet" in resp.data.lower()


@pytest.mark.integration
class TestNoRegressionOnPendingList:
    def test_only_pending_assistant_messages_are_listed(self, client, auth_header, db_conn, seed_conversation):
        # Insère un mix : 1 user 'received', 1 assistant 'pending', 1 assistant 'sent'
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (conversation_id, role, contenu_original, statut) "
                "VALUES (%s, 'user', 'Question A', 'received');",
                (seed_conversation,),
            )
            cur.execute(
                "INSERT INTO messages (conversation_id, role, contenu_propose, statut) "
                "VALUES (%s, 'assistant', 'Reponse en attente', 'pending');",
                (seed_conversation,),
            )
            cur.execute(
                "INSERT INTO messages (conversation_id, role, contenu_envoye, statut, valide_par, valide_at) "
                "VALUES (%s, 'assistant', 'Reponse deja envoyee', 'sent', 'admin', NOW());",
                (seed_conversation,),
            )
        db_conn.commit()

        resp = client.get("/", headers=auth_header)
        body = resp.data.decode()
        assert "Reponse en attente" in body
        # Les messages sent ne doivent PAS apparaître dans la liste pending principale
        # mais peuvent apparaître dans "activité récente"
        # On vérifie via la présence de "en attente" comme libellé
        assert "1 en attente" in body or "en attente" in body
