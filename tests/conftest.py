"""
Fixtures partagées des tests AutoHôte.

Stratégie :
- Une BDD `autohote_test` séparée de la prod (`autohote`), créée et détruite
  une fois par session pytest.
- Le schéma est appliqué depuis `scripts/init_db.sql`.
- Les tables de données sont TRUNCATE entre chaque test (autouse fixture)
  pour garantir l'isolation.
- Le client Flask est instancié contre cette BDD via override d'env vars.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

TEST_DB_NAME = "autohote_test"
INIT_SQL_PATH = Path("/app/scripts/init_db.sql")

# Configure l'env AVANT que `app.py` (Flask) ne soit importé par les tests
os.environ.setdefault("POSTGRES_HOST", "postgres")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ["POSTGRES_DB"] = TEST_DB_NAME
os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "test-password")


def _admin_conn():
    """Connexion à la base 'postgres' admin pour créer/dropper la BDD test."""
    conn = psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname="postgres",
    )
    conn.autocommit = True
    return conn


def _test_conn():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=TEST_DB_NAME,
    )


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """
    Crée la BDD test, applique le schéma, puis nettoie en fin de session.
    Idempotent : drop avant recréation.

    Note : DROP/CREATE DATABASE ne peuvent pas tourner en transaction. On ne
    passe donc PAS par `with conn` (qui ouvre une transaction implicite) ;
    on travaille directement avec un cursor en mode autocommit.
    """
    admin = _admin_conn()  # autocommit=True
    try:
        cur = admin.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME};")
        cur.execute(f"CREATE DATABASE {TEST_DB_NAME};")
        cur.close()
    finally:
        admin.close()

    # Applique le schéma sur la BDD nouvellement créée
    schema = INIT_SQL_PATH.read_text(encoding="utf-8")
    conn = _test_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(schema)
        conn.commit()
    finally:
        conn.close()

    yield

    # Nettoyage final
    admin = _admin_conn()
    try:
        cur = admin.cursor()
        # Coupe les connexions résiduelles avant de dropper
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid();",
            (TEST_DB_NAME,),
        )
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME};")
        cur.close()
    finally:
        admin.close()


@pytest.fixture
def db_conn():
    """Connexion fraîche par test (auto-fermée)."""
    conn = _test_conn()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def reset_data(db_conn):
    """
    Vide les tables de données avant CHAQUE test pour isoler les fixtures.
    Le schéma reste intact.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE messages, conversations, logements, sources RESTART IDENTITY CASCADE;"
        )
    db_conn.commit()


# ---------- Fixtures de seed ----------


@pytest.fixture
def seed_source(db_conn) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (name, type) VALUES ('Airbnb', 'airbnb') RETURNING id;"
        )
        sid = cur.fetchone()[0]
    db_conn.commit()
    return sid


@pytest.fixture
def seed_logement(db_conn, seed_source) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logements
                (source_id, nom, adresse, regles, equipements,
                 wifi_nom, wifi_mdp, heure_checkin, heure_checkout, contact_urgence)
            VALUES (%s, 'Test Logement', '1 rue Test',
                    'Pas de fête', 'WiFi, TV',
                    'TestNet', 'testpwd',
                    '15h00', '11h00', '+33600000000')
            RETURNING id;
            """,
            (seed_source,),
        )
        lid = cur.fetchone()[0]
    db_conn.commit()
    return lid


@pytest.fixture
def seed_conversation(db_conn, seed_logement) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversations
                (logement_id, voyageur_nom, voyageur_langue, thread_id_externe)
            VALUES (%s, 'Marie Test', 'fr', 'thread-test-001')
            RETURNING id;
            """,
            (seed_logement,),
        )
        cid = cur.fetchone()[0]
    db_conn.commit()
    return cid


@pytest.fixture
def seed_pending_pair(db_conn, seed_conversation) -> int:
    """Insère un message user reçu + un brouillon assistant pending.
    Retourne l'id du brouillon."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (conversation_id, role, contenu_original, statut)
            VALUES (%s, 'user', 'Bonjour, à quelle heure le check-in ?', 'received');
            """,
            (seed_conversation,),
        )
        cur.execute(
            """
            INSERT INTO messages (conversation_id, role, contenu_propose, statut)
            VALUES (%s, 'assistant', 'Bonjour ! Le check-in se fait à 15h00.', 'pending')
            RETURNING id;
            """,
            (seed_conversation,),
        )
        draft_id = cur.fetchone()[0]
    db_conn.commit()
    return draft_id


# ---------- Fixtures Flask ----------


@pytest.fixture
def client():
    """Client de test Flask (basic auth fournie via auth_header)."""
    # Import tardif : env vars déjà setées par le module
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth_header():
    """Header HTTP Basic correspondant aux credentials DASHBOARD_*."""
    u = os.environ["DASHBOARD_USER"]
    p = os.environ["DASHBOARD_PASSWORD"]
    token = base64.b64encode(f"{u}:{p}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def bad_auth_header():
    token = base64.b64encode(b"intruder:wrong").decode()
    return {"Authorization": f"Basic {token}"}
