"""Flow de login passwordless Airbnb via code email.

Étapes :
1. Si une session valide existe sur disque → réutiliser, vérifier qu'on est
   toujours loggué en allant sur /hosting. Si oui, retourner.
2. Sinon : naviguer vers la page de login Airbnb, saisir l'email, demander
   le code par email, attendre que le code arrive dans `airbnb_auth_codes`
   (Mailgun → /webhook/email → BDD), le saisir, valider.
3. Sauvegarder `storage_state()` pour les prochains runs.

Usage en CLI (debug) :
    python -m scraper.airbnb_auth login

Programmatic :
    from scraper.airbnb_auth import ensure_logged_in
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ensure_logged_in(browser)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError, sync_playwright

from scraper import db, selectors, session

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

AIRBNB_LOGIN_URL = "https://www.airbnb.fr/login/email"
AIRBNB_HOSTING_URL = "https://www.airbnb.fr/hosting"
DEFAULT_TIMEOUT_MS = 30_000
AUTH_CODE_POLL_TIMEOUT_SEC = 90
AUTH_CODE_POLL_INTERVAL_SEC = 2


def _get_login_email() -> str:
    email = os.environ.get("AIRBNB_LOGIN_EMAIL", "").strip()
    if not email:
        raise RuntimeError(
            "AIRBNB_LOGIN_EMAIL non défini dans l'environnement du conteneur scraper."
        )
    return email


def _fetch_fresh_login_code(after: datetime) -> str | None:
    """Récupère le code login Airbnb le plus récent non consommé, reçu après
    `after`. Marque le code comme consommé pour éviter double usage.

    Retourne None si aucun code valide trouvé.
    """
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, code FROM airbnb_auth_codes
                WHERE consumed_at IS NULL
                  AND received_at >= %s
                ORDER BY received_at DESC
                LIMIT 1;
                """,
                (after,),
            )
            row = cur.fetchone()
            if not row:
                return None
            code_id, code = row
            cur.execute(
                "UPDATE airbnb_auth_codes SET consumed_at = CURRENT_TIMESTAMP "
                "WHERE id = %s;",
                (code_id,),
            )
            conn.commit()
            return code


def _wait_for_auth_code(timeout_sec: int = AUTH_CODE_POLL_TIMEOUT_SEC) -> str:
    """Poll la table airbnb_auth_codes jusqu'à recevoir un code frais."""
    started_at = datetime.now(timezone.utc)
    deadline = time.monotonic() + timeout_sec
    logger.info("Attente du code email Airbnb (timeout %ds)...", timeout_sec)
    while time.monotonic() < deadline:
        code = _fetch_fresh_login_code(after=started_at)
        if code:
            logger.info("Code login Airbnb reçu (longueur=%d).", len(code))
            return code
        time.sleep(AUTH_CODE_POLL_INTERVAL_SEC)
    raise TimeoutError(
        f"Aucun code login Airbnb reçu en {timeout_sec}s. "
        "Vérifier la configuration Mailgun + la route de forwarding."
    )


def _is_currently_logged_in(page: Page) -> bool:
    """Cherche un élément qui prouve qu'on est sur une page d'host loggué."""
    try:
        page.goto(AIRBNB_HOSTING_URL, wait_until="domcontentloaded",
                  timeout=DEFAULT_TIMEOUT_MS)
        page.wait_for_selector(selectors.LOGGED_IN_INDICATOR, timeout=5_000)
        return True
    except TimeoutError:
        return False


def _save_session_screenshot(page: Page, name: str) -> None:
    """Capture l'état du browser pour debug. Best-effort, ignore les erreurs."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = session.screenshot_dir() / f"{ts}_{name}.png"
        page.screenshot(path=str(path), full_page=True)
        logger.info("Screenshot sauvé : %s", path)
    except Exception as e:  # noqa: BLE001
        logger.warning("Échec screenshot : %s", e)


def _perform_login(context: BrowserContext) -> None:
    """Effectue le flow login passwordless email-code complet."""
    email = _get_login_email()
    page = context.new_page()
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except ImportError:
        logger.warning("playwright_stealth non disponible — login sans stealth.")

    logger.info("Navigation vers %s", AIRBNB_LOGIN_URL)
    page.goto(AIRBNB_LOGIN_URL, wait_until="domcontentloaded",
              timeout=DEFAULT_TIMEOUT_MS)

    # Saisie email
    logger.info("Saisie email...")
    page.fill(selectors.LOGIN_EMAIL_INPUT, email)
    page.click(selectors.LOGIN_SUBMIT_EMAIL_BUTTON)

    # Si Airbnb propose plusieurs méthodes de 2FA, on clique l'option email.
    # Best-effort : si l'élément n'apparaît pas, on assume que l'email est
    # la méthode par défaut.
    try:
        page.get_by_text(
            selectors.LOGIN_CHOOSE_EMAIL_BUTTON_TEXT,
            exact=False,
        ).click(timeout=5_000)
        logger.info("Méthode email sélectionnée.")
    except TimeoutError:
        logger.info("Pas de choix de méthode — code email envoyé directement.")

    # Attente du code via Mailgun
    code = _wait_for_auth_code()

    # Saisie du code
    logger.info("Saisie du code...")
    page.fill(selectors.LOGIN_CODE_INPUT, code)
    page.click(selectors.LOGIN_CODE_SUBMIT)

    # Validation : on doit être redirigé vers /hosting ou similaire
    try:
        page.wait_for_selector(selectors.LOGGED_IN_INDICATOR, timeout=15_000)
        logger.info("Login réussi.")
    except TimeoutError:
        _save_session_screenshot(page, "login_failed")
        raise RuntimeError(
            "Login Airbnb échoué : indicateur post-login non trouvé. "
            "Une capture est sauvée dans le volume scraper_data/screenshots/."
        )


def ensure_logged_in(browser: Browser) -> BrowserContext:
    """Garantit qu'on a un BrowserContext loggué à Airbnb. Réutilise la
    session existante si valide, sinon refait un login complet et sauvegarde.

    Retourne le context prêt à l'usage. Le caller est responsable de le close.
    """
    storage = str(session.state_path()) if session.has_saved_state() else None
    context = browser.new_context(storage_state=storage)

    if storage:
        logger.info("Tentative de réutilisation de la session existante.")
        page = context.new_page()
        if _is_currently_logged_in(page):
            page.close()
            return context
        logger.info("Session expirée — relogin nécessaire.")
        page.close()
        context.close()
        context = browser.new_context()  # context propre

    _perform_login(context)
    context.storage_state(path=str(session.state_path()))
    logger.info("Session sauvée vers %s", session.state_path())
    return context


def _cli_login():
    """CLI : `python -m scraper.airbnb_auth login` pour un test manuel."""
    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            ctx = ensure_logged_in(browser)
            logger.info("✓ Logged in. Cookies persistés.")
            ctx.close()
        finally:
            browser.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        _cli_login()
    else:
        print("Usage: python -m scraper.airbnb_auth login")
        sys.exit(1)
