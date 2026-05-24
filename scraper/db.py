"""Connexion BDD autonome pour le scraper.

Lit la configuration depuis les env vars POSTGRES_* (mêmes valeurs que
dashboard mais cycle d'import indépendant).
"""
from __future__ import annotations

import os

import psycopg2
import psycopg2.extras

_DB_CONFIG = None


def _get_config() -> dict:
    global _DB_CONFIG
    if _DB_CONFIG is None:
        _DB_CONFIG = {
            "host": os.environ.get("POSTGRES_HOST", "postgres"),
            "port": int(os.environ.get("POSTGRES_PORT", "5432")),
            "user": os.environ["POSTGRES_USER"],
            "password": os.environ["POSTGRES_PASSWORD"],
            "dbname": os.environ["POSTGRES_DB"],
        }
    return _DB_CONFIG


def db_conn():
    """Retourne une nouvelle connexion psycopg2 (à utiliser en context manager)."""
    return psycopg2.connect(**_get_config())
