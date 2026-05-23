#!/usr/bin/env python3
"""
Insère un message de test (voyageur) + le brouillon Claude associé dans la BDD.

Usage : python3 seed_test_message.py "<conversation_id>" "<message du voyageur>"
Défaut : conversation_id=1, message standard de check-in.

À exécuter sur le VPS depuis ~/autohote/, après que postgres tourne et
que la conversation référencée existe.
"""
import json
import os
import sys
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"

CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
# max_tokens calibré sur la longueur typique des réponses (50-200 tokens) avec marge.
# Une borne basse réduit le coût output ; on peut monter si on observe des troncatures.
CLAUDE_MAX_TOKENS = 500


def load_env():
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def psql(env, query, args=()):
    """Exécute une requête SQL paramétrée et retourne le JSON de la première ligne."""
    full_query = "SELECT row_to_json(t) FROM (" + query + ") t;"
    cmd = [
        "docker", "compose", "-f", str(COMPOSE_FILE),
        "exec", "-T", "postgres",
        "psql", "-U", env["POSTGRES_USER"],
        "-d", env["POSTGRES_DB"],
        "-t", "-A",
        "-v", "ON_ERROR_STOP=1",
    ]
    # On passe les args via psql variables (\set) pour éviter l'injection.
    for i, a in enumerate(args, start=1):
        cmd += ["-v", f"a{i}={a}"]
    cmd += ["-c", full_query]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out = result.stdout.strip()
    return json.loads(out) if out else None


def psql_exec(env, query):
    """Exécute du SQL sans retour structuré (INSERT simple sans RETURNING parsé)."""
    cmd = [
        "docker", "compose", "-f", str(COMPOSE_FILE),
        "exec", "-T", "postgres",
        "psql", "-U", env["POSTGRES_USER"],
        "-d", env["POSTGRES_DB"],
        "-v", "ON_ERROR_STOP=1",
        "-c", query,
    ]
    subprocess.run(cmd, check=True)


def psql_insert_returning_id(env, table, values):
    """INSERT typé avec retour d'id. `values` est un dict colonne -> valeur."""
    cols = ",".join(values.keys())
    placeholders = ",".join([f"${i+1}" for i in range(len(values))])
    args = list(values.values())

    # psql ne supporte pas \bind via -c directement ; on encode les valeurs en
    # littéraux via quote_literal côté SQL, en passant par des variables.
    parts = []
    for i, v in enumerate(args):
        if v is None:
            parts.append("NULL")
        elif isinstance(v, int):
            parts.append(str(v))
        else:
            # Échappement standard PostgreSQL : ' -> ''
            escaped = str(v).replace("'", "''")
            parts.append(f"'{escaped}'")
    sql = f"INSERT INTO {table} ({cols}) VALUES ({','.join(parts)}) RETURNING id;"
    cmd = [
        "docker", "compose", "-f", str(COMPOSE_FILE),
        "exec", "-T", "postgres",
        "psql", "-U", env["POSTGRES_USER"],
        "-d", env["POSTGRES_DB"],
        "-t", "-A",
        "-v", "ON_ERROR_STOP=1",
        "-c", sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    # En -t -A, psql renvoie la valeur RETURNING puis "INSERT 0 N". On prend la première ligne.
    first_line = result.stdout.strip().splitlines()[0]
    return int(first_line)


def call_claude(env, system_prompt, user_message):
    """
    Appelle Claude Messages API avec :
    - prompt caching activé sur le system prompt (fiche logement statique)
      → 90 % de réduction sur l'input cacheable après le 1er appel (TTL 5 min)
    - max_tokens borné pour éviter les réponses anormalement longues
    """
    # Le system prompt est structuré en deux blocs pour le caching :
    # 1. Instructions générales (courtes, modifiables si on tune le ton)
    # 2. Fiche logement (volumineuse, identique entre messages du même logement)
    #    → c'est ce 2e bloc qu'on marque cache_control
    if isinstance(system_prompt, str):
        system_blocks = [{"type": "text", "text": system_prompt,
                          "cache_control": {"type": "ephemeral"}}]
    else:
        # Déjà structuré en blocs (cas d'appel programmatique)
        system_blocks = system_prompt

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
            "x-api-key": env["CLAUDE_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"Erreur Claude API : {e.code} {e.reason}", file=sys.stderr)
        print(e.read().decode(), file=sys.stderr)
        sys.exit(1)


def build_system_prompt(logement):
    """Construit le prompt système à partir d'une fiche logement."""
    return f"""Tu es l'assistant de gestion du logement "{logement['nom']}" situé à {logement['adresse']}.
Tu réponds aux voyageurs de façon chaleureuse, précise et concise.

RÈGLES IMPORTANTES :
- Réponds TOUJOURS dans la langue du message du voyageur
- Ne mentionne jamais que tu es une IA
- Maximum 5 phrases par réponse, sauf si plus de détails nécessaires
- Pas de bullet points excessifs — réponse en prose naturelle

INFORMATIONS SUR LE LOGEMENT :
Description : {logement.get('description') or ''}
Règles : {logement.get('regles') or ''}
Équipements : {logement.get('equipements') or ''}
WiFi : {logement.get('wifi_nom') or ''} / {logement.get('wifi_mdp') or ''}
Accès : {logement.get('code_acces') or 'Non spécifié'}
Check-in : {logement.get('heure_checkin') or ''} | Check-out : {logement.get('heure_checkout') or ''}
Contact d'urgence : {logement.get('contact_urgence') or ''}"""


def main():
    conv_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    user_message = sys.argv[2] if len(sys.argv) > 2 else \
        "Bonjour, à quelle heure puis-je arriver pour le check-in ?"

    env = load_env()

    # 1. Récupère la conversation + son logement
    logement = psql(env, f"""
        SELECT l.*
        FROM logements l
        JOIN conversations c ON c.logement_id = l.id
        WHERE c.id = {conv_id}
    """)
    if not logement:
        print(f"Aucun logement trouvé pour conversation_id={conv_id}", file=sys.stderr)
        sys.exit(1)

    print(f"→ Logement : {logement['nom']}")
    print(f"→ Message voyageur : {user_message[:80]}...")

    # 2. Insère le message du voyageur
    user_msg_id = psql_insert_returning_id(env, "messages", {
        "conversation_id": conv_id,
        "role": "user",
        "contenu_original": user_message,
        "statut": "received",
    })
    print(f"✓ Message voyageur enregistré (id={user_msg_id})")

    # 3. Appelle Claude
    system_prompt = build_system_prompt(logement)
    print("→ Appel Claude...")
    response = call_claude(env, system_prompt, user_message)
    claude_text = response["content"][0]["text"]
    print(f"✓ Réponse Claude reçue ({len(claude_text)} caractères)")

    # 3b. Stats de coût / caching
    usage = response.get("usage", {})
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_status = "HIT" if cache_read > 0 else ("WRITE" if cache_create > 0 else "MISS")
    print(
        f"  tokens : input={inp} | output={out} | "
        f"cache_create={cache_create} | cache_read={cache_read} [{cache_status}]"
    )

    # 4. Insère le brouillon Claude
    draft_id = psql_insert_returning_id(env, "messages", {
        "conversation_id": conv_id,
        "role": "assistant",
        "contenu_propose": claude_text,
        "statut": "pending",
    })
    print(f"✓ Brouillon Claude enregistré (id={draft_id}, statut=pending)")

    # 5. Récapitulatif
    print("\n--- Aperçu du brouillon Claude ---")
    print(claude_text)


if __name__ == "__main__":
    main()
