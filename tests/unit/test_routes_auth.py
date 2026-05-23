"""Tests unitaires de l'authentification HTTP Basic du dashboard."""
import pytest


@pytest.mark.unit
class TestAuthentication:
    def test_root_requires_auth(self, client):
        resp = client.get("/")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers

    def test_root_rejects_bad_credentials(self, client, bad_auth_header):
        resp = client.get("/", headers=bad_auth_header)
        assert resp.status_code == 401

    def test_root_accepts_good_credentials(self, client, auth_header):
        resp = client.get("/", headers=auth_header)
        assert resp.status_code == 200

    def test_action_requires_auth(self, client):
        resp = client.post("/action/1", data={"action": "send"})
        assert resp.status_code == 401

    def test_health_is_public(self, client):
        # /health ne doit PAS exiger d'auth (vérif liveness depuis monitoring)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
