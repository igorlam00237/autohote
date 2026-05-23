"""
File d'attente de jobs asynchrones — wrapper Python autour de la table
`scraper_jobs` en Postgres.

Producteurs : Flask app (webhook email, dashboard actions, cron) appellent
`enqueue()`. Consommateurs : worker scraper (Phase 3) appelle `claim_next()`
puis `mark_completed()` ou `mark_failed()`.

Pattern critique : `claim_next()` utilise `FOR UPDATE SKIP LOCKED` pour garantir
qu'aucun job n'est claim deux fois quand plusieurs workers tournent en parallèle.

Payload : libre (JSONB). Convention par job_type :
  - process_inbound   : {"thread_id_externe": str, "conversation_id": int}
  - send_reply        : {"draft_id": int, "thread_id_externe": str}
  - login             : {} (no payload, déclenche un refresh de session)
  - daily_reconciliation : {}
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg2.extras

# Référence à la fonction db_conn() de app.py — on l'importe au moment de
# l'appel pour éviter les cycles d'import au chargement du module.


# --- Constantes ---

JOB_TYPE_PROCESS_INBOUND = "process_inbound"
JOB_TYPE_SEND_REPLY = "send_reply"
JOB_TYPE_LOGIN = "login"
JOB_TYPE_DAILY_RECONCILIATION = "daily_reconciliation"

ALL_JOB_TYPES = (
    JOB_TYPE_PROCESS_INBOUND,
    JOB_TYPE_SEND_REPLY,
    JOB_TYPE_LOGIN,
    JOB_TYPE_DAILY_RECONCILIATION,
)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# Délais de retry exponentiel après échec d'un job.
# Index = (attempts - 1) : 1ère retry après 60s, 2e après 5min, 3e après 15min.
# Au-delà de len(RETRY_BACKOFF_SECONDS), on dead-letter (status='failed').
RETRY_BACKOFF_SECONDS = (60, 300, 900)


# --- Helpers internes ---

def _db_conn():
    """Délégation à `app.db_conn()` (import paresseux pour éviter les cycles)."""
    from app import db_conn  # noqa: WPS433
    return db_conn()


def _row_to_dict(cursor, row) -> dict:
    return {desc[0]: row[i] for i, desc in enumerate(cursor.description)}


# --- API publique ---

def enqueue(
    job_type: str,
    payload: dict | None = None,
    *,
    conversation_id: int | None = None,
    scheduled_at: datetime | None = None,
    max_attempts: int = 3,
) -> int:
    """Insère un nouveau job en file d'attente, retourne son id.

    - `scheduled_at` : si fourni dans le futur, le job ne sera claim qu'après.
      Défaut : maintenant (UTC).
    - `max_attempts` : nombre total de tentatives avant dead-letter.
    """
    payload_json = json.dumps(payload or {})
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scraper_jobs
                    (job_type, payload, scheduled_at, max_attempts, conversation_id)
                VALUES (%s, %s::jsonb, COALESCE(%s, CURRENT_TIMESTAMP), %s, %s)
                RETURNING id;
                """,
                (job_type, payload_json, scheduled_at, max_attempts, conversation_id),
            )
            job_id = cur.fetchone()[0]
            conn.commit()
            return job_id


def claim_next() -> dict | None:
    """Réclame atomiquement le job pending le plus ancien, le marque `running`
    et incrémente son compteur de tentatives.

    Retourne le dict complet du job (id, job_type, payload, attempts, etc.)
    ou None si aucun job pending n'est arrivé à échéance.

    Sûr face à la concurrence (FOR UPDATE SKIP LOCKED) : si N workers
    appellent en même temps, chacun obtient un job différent.
    """
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE scraper_jobs
                SET status = %s,
                    started_at = CURRENT_TIMESTAMP,
                    attempts = attempts + 1
                WHERE id = (
                    SELECT id FROM scraper_jobs
                    WHERE status = %s
                      AND scheduled_at <= CURRENT_TIMESTAMP
                    ORDER BY scheduled_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING *;
                """,
                (STATUS_RUNNING, STATUS_PENDING),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None


def mark_completed(job_id: int) -> None:
    """Marque un job comme terminé avec succès."""
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scraper_jobs
                SET status = %s,
                    completed_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE id = %s;
                """,
                (STATUS_COMPLETED, job_id),
            )
            conn.commit()


def mark_failed(job_id: int, error: str) -> dict:
    """Gère l'échec d'un job. Si `attempts < max_attempts`, replanifie avec
    backoff exponentiel. Sinon, marque comme `failed` (dead-letter).

    Retourne un dict décrivant l'action prise :
      {"action": "rescheduled", "next_attempt_at": <iso>, "attempts": N}
      {"action": "dead_lettered", "attempts": N}
    """
    truncated = (error or "")[:2000]
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempts, max_attempts FROM scraper_jobs WHERE id = %s;",
                (job_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Job {job_id} introuvable")
            attempts, max_attempts = row

            if attempts >= max_attempts:
                cur.execute(
                    """
                    UPDATE scraper_jobs
                    SET status = %s,
                        completed_at = CURRENT_TIMESTAMP,
                        last_error = %s
                    WHERE id = %s;
                    """,
                    (STATUS_FAILED, truncated, job_id),
                )
                conn.commit()
                return {"action": "dead_lettered", "attempts": attempts}

            # Replanification : on prend le délai correspondant à l'attempt
            # qui vient d'échouer. Si on en a fait 1, on attend RETRY_BACKOFF[0].
            backoff_index = min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)
            delay = RETRY_BACKOFF_SECONDS[backoff_index]
            next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            cur.execute(
                """
                UPDATE scraper_jobs
                SET status = %s,
                    scheduled_at = %s,
                    last_error = %s
                WHERE id = %s;
                """,
                (STATUS_PENDING, next_at, truncated, job_id),
            )
            conn.commit()
            return {
                "action": "rescheduled",
                "next_attempt_at": next_at.isoformat(),
                "attempts": attempts,
            }


def recent_for_conversation(
    conversation_id: int,
    job_type: str,
    within_seconds: int = 60,
) -> bool:
    """Indique si un job (`pending` ou `running`) du type donné existe pour
    cette conversation, créé dans les `within_seconds` dernières secondes.

    Sert au debounce : éviter d'enqueue un PROCESS_INBOUND si un autre
    PROCESS_INBOUND est déjà programmé pour la même conv (cas de plusieurs
    emails Airbnb arrivant rapprochés).
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM scraper_jobs
                WHERE conversation_id = %s
                  AND job_type = %s
                  AND status IN (%s, %s)
                  AND created_at >= %s
                LIMIT 1;
                """,
                (conversation_id, job_type, STATUS_PENDING, STATUS_RUNNING, threshold),
            )
            return cur.fetchone() is not None


def get_status_summary() -> dict[str, Any]:
    """Retourne un résumé de l'état de la file d'attente pour l'endpoint admin.

    Contenu : compteurs par statut, breakdown par job_type, derniers échecs.
    """
    summary = {
        "pending": 0,
        "running": 0,
        "completed_last_24h": 0,
        "failed": 0,
        "by_type": {jt: 0 for jt in ALL_JOB_TYPES},
        "recent_failures": [],
    }
    with _db_conn() as conn:
        with conn.cursor() as cur:
            # Compteurs simples par statut
            cur.execute(
                "SELECT status, count(*) FROM scraper_jobs GROUP BY status;"
            )
            for status, count in cur.fetchall():
                if status == STATUS_PENDING:
                    summary["pending"] = count
                elif status == STATUS_RUNNING:
                    summary["running"] = count
                elif status == STATUS_FAILED:
                    summary["failed"] = count

            # Completed sur 24h
            cur.execute(
                """
                SELECT count(*) FROM scraper_jobs
                WHERE status = %s
                  AND completed_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours';
                """,
                (STATUS_COMPLETED,),
            )
            summary["completed_last_24h"] = cur.fetchone()[0]

            # Pending par job_type
            cur.execute(
                """
                SELECT job_type, count(*) FROM scraper_jobs
                WHERE status = %s GROUP BY job_type;
                """,
                (STATUS_PENDING,),
            )
            for job_type, count in cur.fetchall():
                summary["by_type"][job_type] = count

            # Derniers échecs (10 max)
            cur.execute(
                """
                SELECT id, job_type, last_error, completed_at
                FROM scraper_jobs
                WHERE status = %s
                ORDER BY completed_at DESC NULLS LAST
                LIMIT 10;
                """,
                (STATUS_FAILED,),
            )
            for row in cur.fetchall():
                summary["recent_failures"].append({
                    "id": row[0],
                    "job_type": row[1],
                    "error": (row[2] or "")[:200],
                    "failed_at": row[3].isoformat() if row[3] else None,
                })
    return summary


def get_job(job_id: int) -> dict | None:
    """Récupère un job par son id. Utile pour debug et tests."""
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM scraper_jobs WHERE id = %s;", (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None
