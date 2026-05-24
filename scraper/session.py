"""Persistance de la session Playwright (cookies + localStorage).

Utilise `BrowserContext.storage_state()` natif de Playwright pour sauvegarder
l'état complet d'authentification dans un fichier JSON. Au prochain run, on
recharge ce fichier et on se retrouve loggué sans repasser par l'email-code.
"""
from __future__ import annotations

import os
from pathlib import Path


def state_path() -> Path:
    """Chemin du fichier d'état Playwright. Crée le dossier parent si besoin."""
    base = Path(os.environ.get("SCRAPER_STATE_DIR", "/app/state"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "state.json"


def has_saved_state() -> bool:
    """Y a-t-il un état précédent à recharger ?"""
    return state_path().exists() and state_path().stat().st_size > 0


def screenshot_dir() -> Path:
    """Dossier où le scraper écrit les captures d'écran en cas d'erreur."""
    d = Path(os.environ.get("SCRAPER_STATE_DIR", "/app/state")) / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d
