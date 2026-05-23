"""Tests d'intégration de la file d'attente de jobs (`dashboard/jobs.py`).

Couvre :
- Schéma de la table scraper_jobs
- enqueue avec différents payloads
- claim_next : FIFO, atomicité, FOR UPDATE SKIP LOCKED
- mark_completed / mark_failed (avec retry exponentiel + dead-letter)
- Debounce par conversation
- Endpoint admin /api/jobs/status
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

import jobs


# ============================================================
# Schéma
# ============================================================

@pytest.fixture(autouse=True)
def truncate_jobs_between_tests(db_conn):
    """Vide la table scraper_jobs avant chaque test pour isoler les fixtures."""
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE scraper_jobs RESTART IDENTITY CASCADE;")
    db_conn.commit()


@pytest.mark.integration
class TestJobsSchema:
    def test_table_exists(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='scraper_jobs';"
            )
            assert cur.fetchone() is not None

    REQUIRED_COLUMNS = {
        "id", "job_type", "payload", "status", "attempts", "max_attempts",
        "scheduled_at", "started_at", "completed_at", "last_error",
        "conversation_id", "created_at",
    }

    def test_required_columns_present(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='scraper_jobs';"
            )
            cols = {row[0] for row in cur.fetchall()}
        missing = self.REQUIRED_COLUMNS - cols
        assert not missing, f"Colonnes manquantes : {missing}"

    def test_indexes_present(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename='scraper_jobs';"
            )
            indexes = {row[0] for row in cur.fetchall()}
        assert "idx_scraper_jobs_pending" in indexes
        assert "idx_scraper_jobs_conversation" in indexes


# ============================================================
# enqueue
# ============================================================

@pytest.mark.integration
class TestEnqueue:
    def test_basic_enqueue(self):
        job_id = jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND)
        assert isinstance(job_id, int) and job_id > 0
        job = jobs.get_job(job_id)
        assert job["job_type"] == "process_inbound"
        assert job["status"] == "pending"
        assert job["attempts"] == 0
        assert job["payload"] == {}

    def test_enqueue_with_payload(self):
        payload = {"thread_id_externe": "abc123", "extra": [1, 2, 3]}
        job_id = jobs.enqueue(jobs.JOB_TYPE_SEND_REPLY, payload=payload)
        job = jobs.get_job(job_id)
        assert job["payload"] == payload

    def test_enqueue_with_conversation_id(self, seed_conversation):
        job_id = jobs.enqueue(
            jobs.JOB_TYPE_PROCESS_INBOUND,
            conversation_id=seed_conversation,
        )
        job = jobs.get_job(job_id)
        assert job["conversation_id"] == seed_conversation

    def test_enqueue_scheduled_future(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        job_id = jobs.enqueue(jobs.JOB_TYPE_LOGIN, scheduled_at=future)
        job = jobs.get_job(job_id)
        # Comparaison sur la valeur arrondie à la seconde
        assert abs((job["scheduled_at"] - future).total_seconds()) < 1


# ============================================================
# claim_next
# ============================================================

@pytest.mark.integration
class TestClaim:
    def test_returns_none_when_empty(self):
        assert jobs.claim_next() is None

    def test_returns_oldest_pending(self):
        first = jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND, payload={"order": 1})
        time.sleep(0.01)
        jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND, payload={"order": 2})

        claimed = jobs.claim_next()
        assert claimed["id"] == first
        assert claimed["payload"] == {"order": 1}

    def test_claim_marks_running_and_increments_attempts(self):
        job_id = jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND)
        claimed = jobs.claim_next()
        assert claimed["status"] == "running"
        assert claimed["attempts"] == 1
        assert claimed["started_at"] is not None
        # Persisté en BDD
        re_read = jobs.get_job(job_id)
        assert re_read["status"] == "running"

    def test_skips_jobs_scheduled_in_future(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        jobs.enqueue(jobs.JOB_TYPE_LOGIN, scheduled_at=future)
        assert jobs.claim_next() is None

    def test_concurrent_claims_get_different_jobs(self):
        """Test critique de FOR UPDATE SKIP LOCKED : deux 'workers' qui
        claim en parallèle doivent obtenir des jobs différents, pas le même.
        """
        # Crée 3 jobs
        ids = [jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND) for _ in range(3)]

        # Simule 2 workers en utilisant 2 connexions BDD distinctes ouvertes
        # simultanément. Chaque transaction prend un verrou sur la ligne et
        # SKIP LOCKED fait que le second claim passe au job suivant.
        from app import db_conn as app_db_conn

        conn1 = app_db_conn()
        conn2 = app_db_conn()
        try:
            cur1 = conn1.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur2 = conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            sql = """
                UPDATE scraper_jobs
                SET status = 'running', started_at = CURRENT_TIMESTAMP,
                    attempts = attempts + 1
                WHERE id = (
                    SELECT id FROM scraper_jobs
                    WHERE status = 'pending' AND scheduled_at <= CURRENT_TIMESTAMP
                    ORDER BY scheduled_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                RETURNING id;
            """
            cur1.execute(sql)
            j1 = cur1.fetchone()
            cur2.execute(sql)
            j2 = cur2.fetchone()

            conn1.commit()
            conn2.commit()
        finally:
            conn1.close()
            conn2.close()

        assert j1 is not None and j2 is not None
        assert j1["id"] != j2["id"], "Les 2 workers ont claim le même job (race condition)"
        assert {j1["id"], j2["id"]}.issubset(set(ids))


# ============================================================
# mark_completed
# ============================================================

@pytest.mark.integration
class TestMarkCompleted:
    def test_transitions_to_completed(self):
        job_id = jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND)
        jobs.claim_next()
        jobs.mark_completed(job_id)
        job = jobs.get_job(job_id)
        assert job["status"] == "completed"
        assert job["completed_at"] is not None
        assert job["last_error"] is None


# ============================================================
# mark_failed (retry + dead-letter)
# ============================================================

@pytest.mark.integration
class TestMarkFailed:
    def test_under_max_reschedules_with_backoff(self):
        job_id = jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND, max_attempts=3)
        jobs.claim_next()  # attempts -> 1
        result = jobs.mark_failed(job_id, "transient error")

        assert result["action"] == "rescheduled"
        assert result["attempts"] == 1

        job = jobs.get_job(job_id)
        assert job["status"] == "pending"
        assert job["last_error"] == "transient error"
        # Le scheduled_at est dans le futur (au moins 50s)
        assert job["scheduled_at"] > datetime.now(timezone.utc) + timedelta(seconds=50)

    def test_backoff_increases_with_attempts(self):
        """Vérifie que le backoff suit la séquence 60s → 5min → 15min."""
        job_id = jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND, max_attempts=10)

        # Simule 3 échecs successifs, en avançant attempts manuellement
        delays = []
        for expected_attempts in (1, 2, 3):
            with jobs._db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE scraper_jobs SET attempts = %s, status = 'running' "
                        "WHERE id = %s;",
                        (expected_attempts, job_id),
                    )
                conn.commit()
            now = datetime.now(timezone.utc)
            jobs.mark_failed(job_id, f"attempt {expected_attempts}")
            job = jobs.get_job(job_id)
            delay = (job["scheduled_at"] - now).total_seconds()
            delays.append(delay)

        # Tolérance ±5s pour les variations de timing
        assert 55 <= delays[0] <= 65        # 60s
        assert 295 <= delays[1] <= 305      # 300s
        assert 895 <= delays[2] <= 905      # 900s

    def test_at_max_attempts_dead_letters(self):
        job_id = jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND, max_attempts=2)
        jobs.claim_next()  # attempts -> 1
        jobs.mark_failed(job_id, "first fail")

        # On reclaim et on refail (attempts -> 2 = max_attempts)
        claimed = jobs.claim_next()
        # claim_next() ne va pas reprendre le job avant son scheduled_at,
        # on triche en remettant scheduled_at à NOW
        if claimed is None:
            with jobs._db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE scraper_jobs SET scheduled_at = CURRENT_TIMESTAMP "
                        "WHERE id = %s;",
                        (job_id,),
                    )
                conn.commit()
            jobs.claim_next()

        result = jobs.mark_failed(job_id, "second fail final")
        assert result["action"] == "dead_lettered"

        job = jobs.get_job(job_id)
        assert job["status"] == "failed"
        assert job["last_error"] == "second fail final"

    def test_truncates_long_error(self):
        job_id = jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND)
        jobs.claim_next()
        jobs.mark_failed(job_id, "x" * 5000)
        job = jobs.get_job(job_id)
        assert len(job["last_error"]) == 2000


# ============================================================
# Debounce
# ============================================================

@pytest.mark.integration
class TestDebounce:
    def test_returns_true_when_recent_pending_exists(self, seed_conversation):
        jobs.enqueue(
            jobs.JOB_TYPE_PROCESS_INBOUND,
            conversation_id=seed_conversation,
        )
        assert jobs.recent_for_conversation(
            seed_conversation, jobs.JOB_TYPE_PROCESS_INBOUND, within_seconds=60
        ) is True

    def test_returns_false_when_no_recent_job(self, seed_conversation):
        assert jobs.recent_for_conversation(
            seed_conversation, jobs.JOB_TYPE_PROCESS_INBOUND, within_seconds=60
        ) is False

    def test_returns_false_for_different_job_type(self, seed_conversation):
        jobs.enqueue(jobs.JOB_TYPE_SEND_REPLY, conversation_id=seed_conversation)
        assert jobs.recent_for_conversation(
            seed_conversation, jobs.JOB_TYPE_PROCESS_INBOUND, within_seconds=60
        ) is False

    def test_returns_false_when_job_too_old(self, seed_conversation, db_conn):
        job_id = jobs.enqueue(
            jobs.JOB_TYPE_PROCESS_INBOUND, conversation_id=seed_conversation
        )
        # On vieillit artificiellement le job au-delà de la fenêtre
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE scraper_jobs SET created_at = CURRENT_TIMESTAMP "
                "- INTERVAL '5 minutes' WHERE id = %s;",
                (job_id,),
            )
        db_conn.commit()
        assert jobs.recent_for_conversation(
            seed_conversation, jobs.JOB_TYPE_PROCESS_INBOUND, within_seconds=60
        ) is False


# ============================================================
# Status summary
# ============================================================

@pytest.mark.integration
class TestStatusSummary:
    def test_empty_state(self):
        summary = jobs.get_status_summary()
        assert summary["pending"] == 0
        assert summary["running"] == 0
        assert summary["completed_last_24h"] == 0
        assert summary["failed"] == 0
        assert summary["recent_failures"] == []

    def test_counts_by_status(self):
        # 2 pending, 1 running, 1 completed
        jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND)
        jobs.enqueue(jobs.JOB_TYPE_SEND_REPLY)
        running_id = jobs.enqueue(jobs.JOB_TYPE_LOGIN)
        jobs.claim_next()  # claim premier pending → running
        jobs.claim_next()  # claim second pending → running
        jobs.mark_completed(running_id)  # 1 completed (on triche : on le marque sans claim)

        summary = jobs.get_status_summary()
        # 0 pending (les 3 ont été touchés), 2 running, 1 completed
        assert summary["pending"] == 0
        assert summary["running"] == 2
        assert summary["completed_last_24h"] == 1


# ============================================================
# Endpoint /api/jobs/status
# ============================================================

@pytest.mark.integration
class TestAdminEndpoint:
    def test_requires_auth(self, client):
        resp = client.get("/api/jobs/status")
        assert resp.status_code == 401

    def test_rejects_bad_credentials(self, client, bad_auth_header):
        resp = client.get("/api/jobs/status", headers=bad_auth_header)
        assert resp.status_code == 401

    def test_returns_summary_with_auth(self, client, auth_header):
        jobs.enqueue(jobs.JOB_TYPE_PROCESS_INBOUND)
        resp = client.get("/api/jobs/status", headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "pending" in body
        assert "by_type" in body
        assert body["pending"] == 1
