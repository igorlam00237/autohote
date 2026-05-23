"""Tests d'intégration du script `scripts/seed_test_message.py`.

Vérifie que le pipeline complet user message → Claude → brouillon en BDD
fonctionne. Claude est mocké via une URL alternative pour éviter de
consommer du crédit API.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SEED_SCRIPT = Path("/app/scripts/seed_test_message.py")


def _import_seed_module():
    """Charge le module seed_test_message en isolant son __main__."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("seed_module", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
class TestSeedScriptHelpers:
    def test_build_system_prompt_includes_logement_data(self):
        mod = _import_seed_module()
        logement = {
            "nom": "Mon Studio",
            "adresse": "12 rue X",
            "description": "Studio cosy",
            "regles": "Pas de fete",
            "equipements": "WiFi, TV",
            "wifi_nom": "MyNet",
            "wifi_mdp": "secret123",
            "code_acces": "1234",
            "heure_checkin": "15h00",
            "heure_checkout": "11h00",
            "contact_urgence": "+33600000000",
        }
        prompt = mod.build_system_prompt(logement)
        assert "Mon Studio" in prompt
        assert "12 rue X" in prompt
        assert "MyNet" in prompt
        assert "15h00" in prompt
        assert "Pas de fete" in prompt


@pytest.mark.integration
class TestClaudeCallOptimization:
    """Vérifie les optimisations de coût appliquées sur l'appel Claude API.

    Pas d'appel réseau réel : on intercepte urlopen et on inspecte le body
    JSON envoyé.
    """

    def _capture_request(self, monkeypatch):
        mod = _import_seed_module()
        captured = {}

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({
                    "id": "msg_fake", "type": "message", "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                }).encode()

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse()

        monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
        return mod, captured

    def test_prompt_caching_enabled_on_system(self, monkeypatch):
        """Le system prompt doit être envoyé en blocs avec cache_control."""
        mod, captured = self._capture_request(monkeypatch)
        mod.call_claude({"CLAUDE_API_KEY": "sk-fake"}, "system prompt content", "Hello")

        system = captured["body"]["system"]
        assert isinstance(system, list), "Le system doit être en blocs (liste), pas une string"
        assert any(
            block.get("cache_control", {}).get("type") == "ephemeral"
            for block in system
        ), "Au moins un bloc system doit avoir cache_control: ephemeral"

    def test_max_tokens_is_capped(self, monkeypatch):
        """max_tokens doit être borné raisonnablement (pas 1024+ par défaut)."""
        mod, captured = self._capture_request(monkeypatch)
        mod.call_claude({"CLAUDE_API_KEY": "sk-fake"}, "x", "y")
        assert captured["body"]["max_tokens"] <= 1000

    def test_uses_sonnet_4_6_model(self, monkeypatch):
        """Le modèle doit être Sonnet 4.6 (dernier équilibre qualité/coût)."""
        mod, captured = self._capture_request(monkeypatch)
        mod.call_claude({"CLAUDE_API_KEY": "sk-fake"}, "x", "y")
        assert captured["body"]["model"] == "claude-sonnet-4-6"

    def test_anthropic_version_header_set(self, monkeypatch):
        mod, captured = self._capture_request(monkeypatch)
        mod.call_claude({"CLAUDE_API_KEY": "sk-fake"}, "x", "y")
        # Les headers sont normalisés en title-case par urllib
        headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
        assert headers_lower.get("anthropic-version") == "2023-06-01"


@pytest.mark.integration
class TestSeedScriptDatabaseImpact:
    """Exécute le script en mockant l'appel Claude, vérifie l'état BDD."""

    def test_inserts_user_message_and_draft(self, db_conn, seed_conversation, monkeypatch):
        mod = _import_seed_module()

        # Force la BDD test
        monkeypatch.setattr(mod, "COMPOSE_FILE", Path("/app/docker-compose.yml"))
        # NB: on appelle directement les fonctions Python plutôt que via subprocess,
        # ce qui évite docker compose exec et garantit que la BDD attaquée est `autohote_test`.

        # Mock Claude
        fake_response = {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "Bonjour ! Check-in 15h."}],
            "stop_reason": "end_turn",
        }
        monkeypatch.setattr(mod, "call_claude", lambda env, sp, um: fake_response)

        # Mock psql_insert_returning_id pour qu'il vise la BDD test au lieu de docker exec
        def fake_insert(env, table, values):
            with db_conn.cursor() as cur:
                cols = ",".join(values.keys())
                placeholders = ",".join(["%s"] * len(values))
                cur.execute(
                    f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) RETURNING id;",
                    list(values.values()),
                )
                new_id = cur.fetchone()[0]
            db_conn.commit()
            return new_id

        def fake_psql(env, query, args=()):
            # Retourne la fiche logement pour le conversation_id de test
            with db_conn.cursor() as cur:
                # On extrait juste le SELECT après le FROM
                cur.execute(
                    "SELECT row_to_json(l) FROM (SELECT l.* FROM logements l "
                    "JOIN conversations c ON c.logement_id = l.id WHERE c.id = %s) l;",
                    (seed_conversation,),
                )
                row = cur.fetchone()
            return row[0] if row else None

        monkeypatch.setattr(mod, "psql_insert_returning_id", fake_insert)
        monkeypatch.setattr(mod, "psql", fake_psql)
        monkeypatch.setattr(mod, "load_env", lambda: {
            "POSTGRES_USER": os.environ["POSTGRES_USER"],
            "POSTGRES_PASSWORD": os.environ["POSTGRES_PASSWORD"],
            "POSTGRES_DB": os.environ["POSTGRES_DB"],
            "CLAUDE_API_KEY": "sk-ant-fake",
        })
        monkeypatch.setattr(sys, "argv", ["seed", str(seed_conversation), "Salut !"])

        # Exécute main
        mod.main()

        # Vérifie l'état BDD
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT role, statut, COALESCE(contenu_original, contenu_propose) "
                "FROM messages WHERE conversation_id = %s ORDER BY id;",
                (seed_conversation,),
            )
            rows = cur.fetchall()
        assert len(rows) == 2
        assert rows[0] == ("user", "received", "Salut !")
        assert rows[1] == ("assistant", "pending", "Bonjour ! Check-in 15h.")
