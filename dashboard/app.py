"""
AutoHôte — Dashboard de validation + Webhooks de réception.

Trois rôles :
- UI HTML (`/`, `/action/...`) pour qu'un humain valide les brouillons Claude.
- Webhook JSON (`/webhook/reception`) — entrée générique authentifiée par token
  partagé (`X-Webhook-Secret`). Utilisée pour tests et pour toute source externe
  qui pousserait un message déjà parsé.
- Webhook Mailgun (`/webhook/email`) — endpoint dédié appelé par Mailgun quand
  un email Airbnb arrive via inbound route. Vérifie HMAC, parse, et délègue
  au même pipeline que /webhook/reception.

Pour le MVP : "Envoyer" met juste statut=sent en BDD. L'envoi réel à Airbnb
sera branché plus tard via réponse email (reply-by-email natif Airbnb).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for, Response

import jobs  # noqa: E402 — import après Flask pour respecter la convention du fichier

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("POSTGRES_HOST", "postgres"),
    "port": int(os.environ.get("POSTGRES_PORT", "5432")),
    "user": os.environ["POSTGRES_USER"],
    "password": os.environ["POSTGRES_PASSWORD"],
    "dbname": os.environ["POSTGRES_DB"],
}

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "change-me")

# Secret partagé pour le webhook de réception générique (test, sources internes).
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Signing key Mailgun pour vérifier l'authenticité des emails entrants.
# Disponible dans Mailgun Dashboard → Settings → Webhooks → Webhook Signing Key.
MAILGUN_SIGNING_KEY = os.environ.get("MAILGUN_SIGNING_KEY", "")

# Config appel Claude (cf. ENGINEERING.md §6 — optimisation des coûts)
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 500
# Nombre de messages d'historique injectés dans le prompt Claude.
# 50 couvre >99% des conversations Airbnb réelles (pré-arrivée + séjour + post-départ
# ≈ 5-20 messages typique, jusqu'à 50 en cas de dialogue intense). Au-delà, on
# tronque pour ne pas exploser le coût ni la latence sur les très longs threads.
# Modifiable via la variable d'env CLAUDE_HISTORY_LIMIT.
CLAUDE_HISTORY_LIMIT = int(os.environ.get("CLAUDE_HISTORY_LIMIT", "50"))


def db_conn():
    return psycopg2.connect(**DB_CONFIG)


def check_auth(username: str, password: str) -> bool:
    return username == DASHBOARD_USER and password == DASHBOARD_PASSWORD


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Authentification requise.",
                401,
                {"WWW-Authenticate": 'Basic realm="AutoHôte Dashboard"'},
            )
        return f(*args, **kwargs)
    return wrapped


def fetch_pending_messages():
    sql = """
        SELECT
            m.id                AS draft_id,
            m.contenu_propose   AS draft_text,
            m.created_at        AS draft_created_at,
            c.id                AS conversation_id,
            c.voyageur_nom      AS voyageur_nom,
            c.voyageur_langue   AS voyageur_langue,
            l.nom               AS logement_nom,
            (
                SELECT contenu_original
                FROM messages
                WHERE conversation_id = m.conversation_id
                  AND role = 'user'
                ORDER BY id DESC
                LIMIT 1
            ) AS user_message
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        JOIN logements l ON l.id = c.logement_id
        WHERE m.role = 'assistant' AND m.statut = 'pending'
        ORDER BY m.created_at ASC
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return cur.fetchall()


def fetch_recent_activity(limit: int = 10):
    sql = """
        SELECT
            m.id,
            m.role,
            m.statut,
            m.created_at,
            m.valide_at,
            m.valide_par,
            m.contenu_propose,
            m.contenu_envoye,
            c.voyageur_nom,
            c.voyageur_langue,
            l.nom AS logement_nom,
            (
                SELECT contenu_original
                FROM messages
                WHERE conversation_id = m.conversation_id
                  AND role = 'user'
                  AND id < m.id
                ORDER BY id DESC
                LIMIT 1
            ) AS user_message
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        JOIN logements l ON l.id = c.logement_id
        WHERE m.statut IN ('sent', 'rejected')
        ORDER BY COALESCE(m.valide_at, m.created_at) DESC
        LIMIT %s
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return cur.fetchall()


def update_message_status(draft_id: int, action: str, final_text: str | None, validator: str):
    """Met à jour un brouillon en fonction de l'action choisie par l'humain."""
    now = datetime.now(timezone.utc)
    if action == "send":
        sql = """
            UPDATE messages
            SET statut = 'sent',
                contenu_envoye = COALESCE(%s, contenu_propose),
                valide_par = %s,
                valide_at = %s
            WHERE id = %s AND statut = 'pending'
            RETURNING id
        """
        params = (final_text, validator, now, draft_id)
    elif action == "reject":
        sql = """
            UPDATE messages
            SET statut = 'rejected',
                valide_par = %s,
                valide_at = %s
            WHERE id = %s AND statut = 'pending'
            RETURNING id
        """
        params = (validator, now, draft_id)
    else:
        raise ValueError(f"Action inconnue : {action}")

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            conn.commit()
            return row is not None


@app.route("/")
@require_auth
def index():
    pending = fetch_pending_messages()
    recent = fetch_recent_activity()
    return render_template(
        "index.html",
        pending=pending,
        recent=recent,
        now=datetime.now(timezone.utc),
    )


@app.route("/action/<int:draft_id>", methods=["POST"])
@require_auth
def action(draft_id: int):
    auth = request.authorization
    chosen = request.form.get("action")
    edited = request.form.get("edited_text", "").strip()

    if chosen not in {"send", "reject"}:
        abort(400, "Action inconnue.")

    final_text = edited if (chosen == "send" and edited) else None
    ok = update_message_status(draft_id, chosen, final_text, validator=auth.username)
    if not ok:
        # Probablement déjà traité par un autre onglet, ou id inconnu
        return redirect(url_for("index") + f"?warn=already_handled&id={draft_id}")

    return redirect(url_for("index") + f"?ok={chosen}&id={draft_id}")


# ============================================================
# Webhook de réception (Apify → AutoHôte)
# ============================================================


def build_system_prompt(logement: dict, history_messages: list[dict]) -> list[dict]:
    """
    Construit un system prompt structuré en deux blocs pour activer le
    prompt caching côté Anthropic (cf. ENGINEERING.md §6).

    Bloc 1 : instructions générales (courtes, peuvent évoluer si on tune le ton)
    Bloc 2 : contexte logement + historique (volumineux, identique entre les
             messages d'une même conversation) → marqué cache_control
    """
    instructions = (
        "Tu es un assistant qui répond aux voyageurs d'un logement Airbnb au nom "
        "de l'hôte. Tu réponds de façon chaleureuse, précise et concise.\n\n"
        "RÈGLES IMPORTANTES :\n"
        "- Réponds TOUJOURS dans la langue du message du voyageur\n"
        "- Ne mentionne jamais que tu es une IA\n"
        "- Maximum 5 phrases par réponse, sauf si plus de détails nécessaires\n"
        "- Pas de bullet points excessifs — réponse en prose naturelle"
    )

    context_parts = [
        f'LOGEMENT : "{logement["nom"]}" situé à {logement.get("adresse") or ""}',
        f'Description : {logement.get("description") or ""}',
        f'Règles : {logement.get("regles") or ""}',
        f'Équipements : {logement.get("equipements") or ""}',
        f'WiFi : {logement.get("wifi_nom") or ""} / {logement.get("wifi_mdp") or ""}',
        f'Accès : {logement.get("code_acces") or "Non spécifié"}',
        f'Check-in : {logement.get("heure_checkin") or ""} | '
        f'Check-out : {logement.get("heure_checkout") or ""}',
        f"Contact d'urgence : {logement.get('contact_urgence') or ''}",
    ]

    if history_messages:
        context_parts.append("\nHISTORIQUE DES ÉCHANGES :")
        for m in history_messages:
            who = "Voyageur" if m["role"] == "user" else "Hôte"
            content = m.get("contenu_envoye") or m.get("contenu_propose") or m.get("contenu_original", "")
            context_parts.append(f"- {who} : {content}")

    return [
        {"type": "text", "text": instructions},
        {
            "type": "text",
            "text": "\n".join(context_parts),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def call_claude(system_blocks: list[dict], user_message: str) -> dict:
    """Appel direct à Claude Messages API. Lève en cas d'erreur."""
    if not CLAUDE_API_KEY:
        raise RuntimeError("CLAUDE_API_KEY non configurée")
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_message}],
    }
    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def find_or_create_conversation(
    cur, *, logement_id: int, thread_id_externe: str | None,
    voyageur_nom: str, voyageur_langue: str | None,
    airbnb_listing_id: str | None = None,
) -> int:
    """Retourne l'id d'une conversation existante (matchée par thread_id_externe
    + logement_id), ou en crée une nouvelle. Met aussi à jour airbnb_listing_id
    si fourni et qu'il n'était pas encore stocké."""
    if thread_id_externe:
        cur.execute(
            "SELECT id, airbnb_listing_id FROM conversations "
            "WHERE thread_id_externe = %s AND logement_id = %s;",
            (thread_id_externe, logement_id),
        )
        row = cur.fetchone()
        if row:
            conv_id, existing_listing_id = row
            # Si on a découvert le listing_id maintenant et qu'il manquait, on le complète
            if airbnb_listing_id and not existing_listing_id:
                cur.execute(
                    "UPDATE conversations SET airbnb_listing_id = %s WHERE id = %s;",
                    (airbnb_listing_id, conv_id),
                )
            return conv_id

    cur.execute(
        """
        INSERT INTO conversations
            (logement_id, voyageur_nom, voyageur_langue, thread_id_externe, airbnb_listing_id)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (logement_id, voyageur_nom, voyageur_langue, thread_id_externe, airbnb_listing_id),
    )
    return cur.fetchone()[0]


def load_logement(cur, logement_id: int) -> dict | None:
    cur.execute(
        "SELECT * FROM logements WHERE id = %s AND actif = 1;",
        (logement_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def load_history(cur, conversation_id: int, limit: int) -> list[dict]:
    """Charge les N derniers messages de la conversation, ordre chronologique."""
    cur.execute(
        """
        SELECT role, contenu_original, contenu_propose, contenu_envoye, created_at
        FROM (
            SELECT role, contenu_original, contenu_propose, contenu_envoye, created_at
            FROM messages
            WHERE conversation_id = %s
              AND (role = 'user' OR statut = 'sent')
            ORDER BY id DESC
            LIMIT %s
        ) recent
        ORDER BY created_at ASC;
        """,
        (conversation_id, limit),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _check_webhook_secret() -> bool:
    if not WEBHOOK_SECRET:
        return False
    provided = request.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(provided, WEBHOOK_SECRET)


def process_incoming_message(
    *,
    logement_id: int,
    message_text: str,
    thread_id_externe: str | None = None,
    external_message_id: str | None = None,
    voyageur_nom: str = "Voyageur",
    voyageur_langue: str | None = None,
    airbnb_listing_id: str | None = None,
    reply_to_address: str | None = None,
) -> tuple[dict, int]:
    """Pipeline réutilisable : trouve/crée conversation, enregistre user msg,
    charge contexte, appelle Claude, enregistre brouillon.

    Retourne (payload_dict, http_status_code).
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            # Idempotence
            if external_message_id:
                cur.execute(
                    "SELECT id, conversation_id FROM messages "
                    "WHERE external_message_id = %s;",
                    (external_message_id,),
                )
                existing = cur.fetchone()
                if existing:
                    user_msg_id, conv_id = existing
                    cur.execute(
                        "SELECT id FROM messages "
                        "WHERE conversation_id = %s AND role = 'assistant' "
                        "ORDER BY id DESC LIMIT 1;",
                        (conv_id,),
                    )
                    draft_row = cur.fetchone()
                    return {
                        "status": "ok",
                        "already_processed": True,
                        "conversation_id": conv_id,
                        "user_message_id": user_msg_id,
                        "draft_id": draft_row[0] if draft_row else None,
                    }, 200

            logement = load_logement(cur, logement_id)
            if not logement:
                return {
                    "status": "error",
                    "error": f"logement_id {logement_id} inconnu ou inactif"
                }, 404

            conv_id = find_or_create_conversation(
                cur,
                logement_id=logement_id,
                thread_id_externe=thread_id_externe,
                voyageur_nom=voyageur_nom,
                voyageur_langue=voyageur_langue,
                airbnb_listing_id=airbnb_listing_id,
            )
            history = load_history(cur, conv_id, CLAUDE_HISTORY_LIMIT)

            cur.execute(
                """
                INSERT INTO messages
                    (conversation_id, role, contenu_original, statut, external_message_id)
                VALUES (%s, 'user', %s, 'received', %s)
                RETURNING id;
                """,
                (conv_id, message_text, external_message_id),
            )
            user_msg_id = cur.fetchone()[0]
            conn.commit()

    try:
        system_blocks = build_system_prompt(logement, history)
        claude_response = call_claude(system_blocks, message_text)
        claude_text = claude_response["content"][0]["text"]
    except urllib.error.HTTPError as e:
        app.logger.error("Claude API error: %s", e.read().decode(errors="replace"))
        return {
            "status": "partial",
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
            "draft_id": None,
            "error": "claude_api_error",
        }, 502
    except Exception as e:
        app.logger.exception("Échec appel Claude")
        return {
            "status": "partial",
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
            "draft_id": None,
            "error": str(e),
        }, 500

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages
                    (conversation_id, role, contenu_propose, statut)
                VALUES (%s, 'assistant', %s, 'pending')
                RETURNING id;
                """,
                (conv_id, claude_text),
            )
            draft_id = cur.fetchone()[0]
            conn.commit()

    return {
        "status": "ok",
        "already_processed": False,
        "conversation_id": conv_id,
        "user_message_id": user_msg_id,
        "draft_id": draft_id,
    }, 200


@app.route("/webhook/reception", methods=["POST"])
def webhook_reception():
    """Point d'entrée pour qu'une source externe pousse un nouveau message
    voyageur. Auth par token partagé dans le header X-Webhook-Secret.

    Body JSON attendu :
        {
          "logement_id": 1,
          "thread_id_externe": "abc-123",       (optionnel mais recommandé)
          "voyageur_nom": "Marie Dupont",
          "voyageur_langue": "fr",              (ISO 639-1, optionnel)
          "message_text": "Bonjour, ...",
          "external_message_id": "msg-uuid"     (optionnel, recommandé pour idempotence)
        }

    Réponse 200 :
        {"status": "ok", "conversation_id": N, "user_message_id": N,
         "draft_id": N, "already_processed": false}
    """
    if not _check_webhook_secret():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    logement_id = payload.get("logement_id")
    message_text = (payload.get("message_text") or "").strip()
    thread_id_externe = payload.get("thread_id_externe")
    external_message_id = payload.get("external_message_id")
    voyageur_nom = payload.get("voyageur_nom") or "Voyageur"
    voyageur_langue = payload.get("voyageur_langue")

    if not isinstance(logement_id, int) or not message_text:
        return jsonify({
            "status": "error",
            "error": "logement_id (int) et message_text (str non vide) sont requis"
        }), 400

    result, code = process_incoming_message(
        logement_id=logement_id,
        message_text=message_text,
        thread_id_externe=thread_id_externe,
        external_message_id=external_message_id,
        voyageur_nom=voyageur_nom,
        voyageur_langue=voyageur_langue,
    )
    return jsonify(result), code


# ============================================================
# Webhook Mailgun (emails Airbnb → AutoHôte)
# ============================================================


# Domaines connus d'Airbnb pour les emails sortants (filtre anti-spam)
AIRBNB_SENDER_DOMAINS = ("airbnb.com", "airbnb.fr", "mail.airbnb.com")


def _verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    """Vérifie l'authenticité d'un webhook Mailgun via HMAC-SHA256.

    cf. https://documentation.mailgun.com/docs/mailgun/user-manual/webhooks/securing-webhooks
    """
    if not MAILGUN_SIGNING_KEY or not (token and timestamp and signature):
        return False
    expected = hmac.new(
        key=MAILGUN_SIGNING_KEY.encode("utf-8"),
        msg=f"{timestamp}{token}".encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _is_from_airbnb(sender: str, from_header: str) -> bool:
    """Filtre simple : on n'accepte que les emails provenant d'Airbnb."""
    candidates = (sender or "").lower() + " " + (from_header or "").lower()
    return any(d in candidates for d in AIRBNB_SENDER_DOMAINS)


def _parse_mailgun_headers(form: dict) -> dict:
    """Parse le champ 'message-headers' (JSON-encoded liste de [name, value])
    que Mailgun envoie. Retourne un dict {Header-Name: value}.

    Mailgun fournit aussi certains headers comme champs top-level du form
    (Subject, From, To, Message-Id, In-Reply-To, References) — on les fusionne
    pour avoir une vue complète.
    """
    parsed = {}
    raw = form.get("message-headers")
    if raw:
        try:
            entries = json.loads(raw)
            for entry in entries:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    parsed[entry[0]] = entry[1]
        except (json.JSONDecodeError, TypeError):
            pass
    # Ajoute les headers utiles que Mailgun met aussi en top-level
    for k in ("Subject", "From", "To", "Reply-To", "Message-Id",
              "In-Reply-To", "References"):
        if k in form and k not in parsed:
            parsed[k] = form[k]
    return parsed


def _is_airbnb_message_email(headers: dict) -> bool:
    """Vrai si l'email Airbnb est un VRAI message voyageur (pas rappel,
    pas notification de paiement, pas check-in reminder).

    On se base sur les headers propriétaires Airbnb :
    - X-Category: 'message' pour les nouveaux messages dans une conversation
    - X-Template: doit commencer par 'MESSAGING_' (vs RESERVATION_, REMINDER_, etc.)
    """
    category = (headers.get("X-Category") or "").strip().lower()
    if category != "message":
        return False
    template = (headers.get("X-Template") or "").strip().upper()
    if not template.startswith("MESSAGING_"):
        return False
    return True


def _is_airbnb_login_email(headers: dict, subject: str = "") -> bool:
    """Vrai si l'email Airbnb contient un code de connexion (login email-code).

    Détecté soit par X-Template commençant par LOGIN_ / AUTH_ / VERIFY_,
    soit par patterns connus dans le sujet (multilingue).
    """
    template = (headers.get("X-Template") or "").strip().upper()
    if template.startswith(("LOGIN_", "AUTH_", "VERIFY_", "VERIFICATION_")):
        return True
    # Fallback subject : on a vu plusieurs formats selon la locale
    subj = (subject or "").lower()
    needles = (
        "code de connexion", "code de vérification", "votre code",
        "verification code", "login code", "your code", "sign-in code",
        "connecter à votre compte",
    )
    return any(n in subj for n in needles)


# Regex pour extraire un code à 4-8 chiffres du body de l'email Airbnb
_AIRBNB_LOGIN_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def _extract_login_code(body: str) -> str | None:
    """Extrait le code numérique d'un email de login Airbnb.

    Hypothèse : le code est la première séquence de 4-8 chiffres isolée
    dans le body (pas une partie d'un nombre plus grand).
    """
    if not body:
        return None
    m = _AIRBNB_LOGIN_CODE_RE.search(body)
    return m.group(1) if m else None


# Regex pré-compilées pour l'extraction depuis le body de l'email Airbnb
_AIRBNB_THREAD_ID_RE = re.compile(r"airbnb\.[a-z.]+/hosting/thread/(\d+)")
_AIRBNB_LISTING_ID_RE = re.compile(r"airbnb\.[a-z.]+/rooms/(\d+)")
# Bloc voyageur dans le body texte : nom en UPPERCASE seul sur sa ligne,
# suivi de "Responsable de la réservation"
_AIRBNB_VOYAGEUR_RE = re.compile(
    r"^\s*([A-ZÀ-ÿ][A-ZÀ-ÿ '\-]+?)\s*\n\s*Responsable de la r[ée]servation",
    re.MULTILINE,
)
# Message texte : tout ce qui suit "Responsable de la réservation" jusqu'à
# "Consulter", "Répondre", ou un lien Airbnb
_AIRBNB_MESSAGE_RE = re.compile(
    r"Responsable de la r[ée]servation\s*\n(.+?)(?=\n\s*(?:Consulter|R[ée]pondre|Vous pouvez|https?://www\.airbnb))",
    re.DOTALL,
)


def _extract_airbnb_thread_id(body: str) -> str | None:
    """Extrait le conversation_id Airbnb canonique depuis une URL
    /hosting/thread/<id> ou /hosting/messages/<id> présente dans le body.
    Stable entre tous les messages d'une même conversation.
    """
    if not body:
        return None
    m = _AIRBNB_THREAD_ID_RE.search(body)
    return m.group(1) if m else None


def _extract_airbnb_listing_id(body: str) -> str | None:
    """Extrait l'ID de l'annonce Airbnb (URL /rooms/<id>) depuis le body."""
    if not body:
        return None
    m = _AIRBNB_LISTING_ID_RE.search(body)
    return m.group(1) if m else None


def _extract_voyageur_from_body(body: str) -> str | None:
    """Extrait le nom du voyageur depuis le bloc UPPERCASE 'ADRIEN\\nResponsable de la réservation'.
    Plus fiable que le From header (souvent générique 'Airbnb <express@...>').
    """
    if not body:
        return None
    m = _AIRBNB_VOYAGEUR_RE.search(body)
    if not m:
        return None
    raw = m.group(1).strip()
    # Le nom est en MAJUSCULES dans l'email. On re-capitalise proprement.
    return " ".join(word.capitalize() for word in raw.split())


def _extract_message_from_body(body: str) -> str | None:
    """Extrait le texte du message voyageur depuis le body Airbnb."""
    if not body:
        return None
    m = _AIRBNB_MESSAGE_RE.search(body)
    if not m:
        return None
    text = m.group(1).strip()
    # Normalise les espaces et les retours à la ligne
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_voyageur_name(from_header: str, subject: str) -> str:
    """
    Tente d'extraire le nom du voyageur :
    - en priorité, le nom complet présent dans le header From: "Marie Dupont <auto@airbnb.com>"
    - sinon, le premier nom mentionné dans le sujet
    - défaut : "Voyageur"
    """
    if from_header:
        match = re.match(r'^\s*"?([^"<]+?)"?\s*<', from_header)
        if match:
            name = match.group(1).strip()
            # Airbnb met souvent "Marie via Airbnb" ou "Airbnb" en From — on filtre
            cleaned = re.sub(r"\s*(via\s+Airbnb|Airbnb)\s*$", "", name, flags=re.IGNORECASE).strip()
            if cleaned and cleaned.lower() not in ("airbnb", "automated", "express", "response"):
                return cleaned

    if subject:
        # Patterns typiques : "Marie sent you a message", "Nouveau message de Marie", etc.
        for pattern in (
            r"^([^:]+?)\s+(?:sent you a message|vous a envoyé|sent a message)",
            r"(?:message from|message de|message envoyé par)\s+([^:.,]+)",
            r"^([A-ZÀ-Ÿ][a-zà-ÿ]+(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ]+)?)\s+:",
        ):
            m = re.search(pattern, subject, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()

    return "Voyageur"


def _extract_thread_id(form: dict) -> str | None:
    """Identifie un thread stable à partir des headers RFC d'un email.

    Priorité (corrigée — cf. RFC 2822 §3.6.4 et standard mail threading) :
    1. References[0] : c'est le Message-Id du tout premier email du thread,
       conservé identique dans TOUS les emails ultérieurs de la conversation.
       → identité stable de la conversation sur la durée.
    2. In-Reply-To : pointe vers l'email immédiatement précédent (et seulement
       celui-là). Utilisé en fallback si References est absent (cas du 1er reply
       où certains clients ne renseignent que In-Reply-To).
    3. Message-Id : utilisé comme identité d'un thread nouvellement créé
       (premier email, pas de prédecesseur).
    """
    references = (form.get("References") or "").strip()
    if references:
        first = references.split()[0].strip()
        if first:
            return first
    in_reply_to = (form.get("In-Reply-To") or "").strip()
    if in_reply_to:
        return in_reply_to
    message_id = (form.get("Message-Id") or form.get("message-id") or "").strip()
    return message_id or None


def _default_logement_id() -> int | None:
    """Pour le MVP, on associe à l'unique logement actif (id le plus petit).
    Plus tard : matching par sujet ou recipient address dédiée par logement.
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM logements WHERE actif = 1 ORDER BY id LIMIT 1;")
            row = cur.fetchone()
            return row[0] if row else None


def _resolve_logement_id(airbnb_listing_id: str | None) -> int | None:
    """Détermine quel logement de notre BDD correspond à cet email Airbnb.

    Stratégie :
    1. Si on a un `airbnb_listing_id` extrait du body, on cherche un logement
       qui aurait ce listing_id dans son `config_json` (clé 'airbnb_listing_id').
       Match précis pour multi-logement.
    2. Sinon, fallback : unique logement actif (cas MVP avec 1 seul logement).

    Retourne None si aucun logement actif.
    """
    if airbnb_listing_id:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM logements
                    WHERE actif = 1
                      AND config_json::jsonb ->> 'airbnb_listing_id' = %s
                    LIMIT 1;
                    """,
                    (airbnb_listing_id,),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
    return _default_logement_id()


@app.route("/webhook/email", methods=["POST"])
def webhook_email():
    """Endpoint appelé par Mailgun quand un email atterrit sur la route
    configurée. Vérifie la signature HMAC, filtre via les headers Airbnb
    (X-Category, X-Template), parse précisément depuis le body, et délègue
    au pipeline standard.

    Stratégie de filtrage (par ordre) :
      1. HMAC signature valide (Mailgun)
      2. Sender appartient à un domaine Airbnb
      3. Header X-Category == 'message' (vs reminder, payment, etc.)
      4. Header X-Template commence par 'MESSAGING_'
      5. Le body contient un thread_id Airbnb extractible

    Le `thread_id_externe` utilisé est l'ID canonique Airbnb extrait
    depuis l'URL `/hosting/thread/<id>` ou `/hosting/messages/<id>` dans
    le body. C'est ce même ID qu'on retrouvera dans l'URL navigateur
    quand le scraper ira poster une réponse.
    """
    form = request.form.to_dict()

    # 1. Signature Mailgun
    if not _verify_mailgun_signature(
        token=form.get("token", ""),
        timestamp=form.get("timestamp", ""),
        signature=form.get("signature", ""),
    ):
        return jsonify({"status": "error", "error": "invalid_signature"}), 401

    headers = _parse_mailgun_headers(form)
    sender = form.get("sender", "")
    from_header = headers.get("From") or form.get("from", "")
    subject = headers.get("Subject") or form.get("subject", "")
    stripped_text = (form.get("stripped-text") or form.get("body-plain") or "").strip()
    body_for_extract = stripped_text or form.get("body-html", "")
    message_id = headers.get("Message-Id") or form.get("Message-Id") or form.get("message-id", "")
    reply_to = headers.get("Reply-To") or form.get("Reply-To", "")

    # 2. Sender domain
    if not _is_from_airbnb(sender, from_header):
        app.logger.warning(
            "Email rejeté (non-Airbnb) : sender=%r from=%r", sender, from_header
        )
        return jsonify({"status": "ignored", "reason": "not_from_airbnb"}), 200

    # 3a. Cas spécial : email de code de connexion Airbnb → stocke en BDD
    if _is_airbnb_login_email(headers, subject):
        code = _extract_login_code(body_for_extract)
        if not code:
            app.logger.warning("Email login Airbnb détecté mais code non extractible.")
            return jsonify({
                "status": "ignored",
                "reason": "login_email_without_code",
                "template": headers.get("X-Template"),
            }), 200
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO airbnb_auth_codes (code, raw_subject, raw_template)
                    VALUES (%s, %s, %s)
                    RETURNING id;
                    """,
                    (code, subject[:200], headers.get("X-Template")),
                )
                code_id = cur.fetchone()[0]
                conn.commit()
        app.logger.info("Code login Airbnb capté (id=%s)", code_id)
        return jsonify({
            "status": "ok",
            "type": "airbnb_login_code",
            "code_id": code_id,
        }), 200

    # 3b. Type d'email Airbnb : doit être un VRAI message voyageur
    if not _is_airbnb_message_email(headers):
        app.logger.info(
            "Email Airbnb non-message ignoré : X-Category=%r X-Template=%r subject=%r",
            headers.get("X-Category"), headers.get("X-Template"), subject[:80],
        )
        return jsonify({
            "status": "ignored",
            "reason": "not_a_message_email",
            "category": headers.get("X-Category"),
            "template": headers.get("X-Template"),
        }), 200

    if not body_for_extract:
        return jsonify({"status": "ignored", "reason": "empty_body"}), 200

    # 4. Extraction depuis le body Airbnb
    airbnb_thread_id = _extract_airbnb_thread_id(body_for_extract)
    airbnb_listing_id = _extract_airbnb_listing_id(body_for_extract)
    voyageur_from_body = _extract_voyageur_from_body(body_for_extract)
    message_from_body = _extract_message_from_body(body_for_extract)

    # Si on n'a pas pu extraire le thread_id Airbnb, on tombe sur le fallback
    # message-id (mais on logue car c'est un signal de format inattendu)
    thread_id_externe = airbnb_thread_id
    if not thread_id_externe:
        app.logger.warning(
            "Pas de thread Airbnb extractible — fallback Message-Id. Subject=%r",
            subject[:80],
        )
        thread_id_externe = _extract_thread_id(form)

    # Nom voyageur : on privilégie le bloc body (UPPERCASE), fallback From/Subject
    voyageur_nom = voyageur_from_body or _extract_voyageur_name(from_header, subject)

    # Texte du message : on privilégie l'extraction propre, fallback stripped-text complet
    message_text = message_from_body or stripped_text

    # 5. Logement : on essaie de matcher par airbnb_listing_id, sinon default
    logement_id = _resolve_logement_id(airbnb_listing_id)
    if logement_id is None:
        return jsonify({"status": "error", "error": "no_active_logement"}), 503

    # 6. Délégation au pipeline standard
    result, code = process_incoming_message(
        logement_id=logement_id,
        message_text=message_text,
        thread_id_externe=thread_id_externe,
        external_message_id=message_id or None,
        voyageur_nom=voyageur_nom,
        voyageur_langue=None,
        airbnb_listing_id=airbnb_listing_id,
        reply_to_address=reply_to or None,
    )

    result["parsed"] = {
        "voyageur_nom": voyageur_nom,
        "thread_id_externe": thread_id_externe,
        "airbnb_listing_id": airbnb_listing_id,
        "logement_id": logement_id,
        "category": headers.get("X-Category"),
        "template": headers.get("X-Template"),
        "subject": subject[:120],
    }
    return jsonify(result), code


@app.route("/health")
def health():
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"status": "ok"}, 200
    except Exception as e:
        return {"status": "error", "detail": str(e)}, 500


@app.route("/api/jobs/status")
@require_auth
def api_jobs_status():
    """Endpoint admin : état de la file d'attente du scraper.

    Protégé par Basic Auth (même credentials que le dashboard). Renvoie les
    compteurs par statut, le breakdown par job_type, et les derniers échecs.
    """
    try:
        return jsonify(jobs.get_status_summary()), 200
    except Exception as e:
        app.logger.exception("Échec récupération status jobs")
        return jsonify({"error": str(e)}), 500


@app.template_filter("humantime")
def humantime(dt: datetime) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"il y a {seconds}s"
    if seconds < 3600:
        return f"il y a {seconds // 60} min"
    if seconds < 86400:
        return f"il y a {seconds // 3600} h"
    return dt.strftime("%d/%m %H:%M")


@app.template_filter("flag")
def language_flag(lang: str) -> str:
    return {
        "fr": "🇫🇷",
        "en": "🇬🇧",
        "es": "🇪🇸",
        "de": "🇩🇪",
        "it": "🇮🇹",
        "pt": "🇵🇹",
        "nl": "🇳🇱",
    }.get((lang or "").lower(), "🌐")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
