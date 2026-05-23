"""Tests d'intégration du schéma BDD.

Garantit que les tables existent avec les colonnes attendues, et que les
contraintes (FK, defaults) sont en place. Premier rempart contre une
migration cassée.
"""
import pytest


@pytest.mark.integration
class TestSchemaTables:
    REQUIRED_TABLES = {
        "sources",
        "logements",
        "conversations",
        "messages",
        "scraper_jobs",
    }

    def test_all_required_tables_exist(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public';"
            )
            present = {row[0] for row in cur.fetchall()}
        missing = self.REQUIRED_TABLES - present
        assert not missing, f"Tables manquantes : {missing}"


@pytest.mark.integration
class TestSchemaColumns:
    EXPECTED = {
        "sources": {"id", "name", "type", "config_json", "actif", "created_at"},
        "logements": {
            "id", "source_id", "nom", "adresse", "description",
            "regles", "equipements", "wifi_nom", "wifi_mdp",
            "code_acces", "heure_checkin", "heure_checkout",
            "contact_urgence", "config_json", "actif", "created_at",
        },
        "conversations": {
            "id", "logement_id", "voyageur_nom", "voyageur_langue",
            "thread_id_externe", "airbnb_listing_id",
            "statut", "created_at", "updated_at",
        },
        "messages": {
            "id", "conversation_id", "role", "contenu_original",
            "contenu_propose", "contenu_envoye", "valide_par",
            "valide_at", "statut", "external_message_id", "created_at",
        },
        "scraper_jobs": {
            "id", "job_type", "payload", "status",
            "attempts", "max_attempts", "scheduled_at",
            "started_at", "completed_at", "last_error",
            "conversation_id", "created_at",
        },
    }

    @pytest.mark.parametrize("table,expected_cols", list(EXPECTED.items()))
    def test_table_has_expected_columns(self, db_conn, table, expected_cols):
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s;",
                (table,),
            )
            present = {row[0] for row in cur.fetchall()}
        missing = expected_cols - present
        assert not missing, f"Colonnes manquantes dans {table} : {missing}"


@pytest.mark.integration
class TestForeignKeys:
    def test_messages_fk_blocks_orphan(self, db_conn):
        """Insérer un message avec conversation_id inexistante doit échouer."""
        from psycopg2 import errors
        with pytest.raises((errors.ForeignKeyViolation, Exception)):
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (conversation_id, role, statut) "
                    "VALUES (99999, 'user', 'received');"
                )
            db_conn.commit()
        db_conn.rollback()


@pytest.mark.integration
class TestDefaults:
    def test_messages_default_statut_is_pending(self, db_conn, seed_conversation):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (conversation_id, role) VALUES (%s, 'assistant') "
                "RETURNING statut;",
                (seed_conversation,),
            )
            statut = cur.fetchone()[0]
        db_conn.commit()
        assert statut == "pending"

    def test_conversations_default_statut_is_active(self, db_conn, seed_logement):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (logement_id, voyageur_nom) "
                "VALUES (%s, 'X') RETURNING statut;",
                (seed_logement,),
            )
            statut = cur.fetchone()[0]
        db_conn.commit()
        assert statut == "active"

    def test_sources_default_actif_is_1(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sources (name, type) VALUES ('Test', 'test') "
                "RETURNING actif;"
            )
            actif = cur.fetchone()[0]
        db_conn.commit()
        assert actif == 1


@pytest.mark.integration
class TestSchemaIdempotency:
    def test_init_sql_can_run_twice(self, db_conn):
        """Le script d'init doit pouvoir être relancé sans erreur."""
        from pathlib import Path
        sql = Path("/app/scripts/init_db.sql").read_text(encoding="utf-8")
        with db_conn.cursor() as cur:
            cur.execute(sql)
        db_conn.commit()
        # Second run
        with db_conn.cursor() as cur:
            cur.execute(sql)
        db_conn.commit()
