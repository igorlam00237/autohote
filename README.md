# AutoHôte

Outil d'automatisation des conversations Airbnb avec validation humaine.

Réduit le temps de réponse aux voyageurs de ~20 min à ~30 sec par message en générant un brouillon de réponse à partir d'une fiche logement structurée et de l'historique de la conversation. Chaque réponse passe par une validation humaine via un dashboard web avant envoi.

---

## Stack

| Composant | Technologie | Rôle |
|---|---|---|
| Conteneurisation | Docker + Docker Compose | 5 services orchestrés |
| Reverse proxy + TLS | Caddy 2 | Ports 80/443, certificat Let's Encrypt automatique |
| DNS dynamique | DuckDNS | Sous-domaine gratuit pointant sur l'IP du VPS |
| Réception des messages | Mailgun (Inbound Routes) | Capture des notifications email Airbnb, webhook HMAC signé |
| Dashboard validation + endpoints HTTP | Flask + Gunicorn | Port 8080, HTTP Basic Auth |
| Scraping Airbnb (read/write) | Playwright + Chromium headless | Lecture conversations, envoi réponses |
| Base de données | PostgreSQL 16 | Logements, conversations, messages, file d'attente jobs |
| Génération de réponses | Anthropic Claude API (`claude-sonnet-4-6`) | Prompt caching activé |
| Orchestration workflows planifiés | n8n self-hosted | ⚠️ Conteneur installé, **non utilisé actuellement** — réservé pour les séquences automatiques (J-2 / J0 / J+1) de la V1 |
| Hébergement cible | VPS Ubuntu 22.04 | Hostinger KVM1 |

---

## Architecture

Le flux principal : un voyageur envoie un message sur Airbnb, qui notifie l'hôte par email. Cet email est redirigé vers Mailgun, qui POST le contenu vers le webhook Flask. Flask parse l'email, l'enregistre en BDD, appelle Claude pour générer un brouillon, et l'affiche sur le dashboard de validation. L'hôte clique pour envoyer/modifier/rejeter.

```
                ┌──────────────────────────────────────┐
                │  AIRBNB                              │
                │  Voyageur envoie un message          │
                │  → notification email à l'hôte       │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  Boîte mail hôte (Outlook/Gmail)     │
                │  Règle "Rediriger" → adresse Mailgun │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  MAILGUN Inbound Route               │
                │  POST signé HMAC                     │
                │  → https://[domaine]/webhook/email   │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  CADDY (TLS, Let's Encrypt auto)     │
                │  Reverse proxy → dashboard:8080      │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  FLASK /webhook/email                │
                │  - Vérifie signature HMAC            │
                │  - Filtre : seuls emails Airbnb      │
                │  - Extrait voyageur, thread_id,      │
                │    listing_id, contenu                │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  POSTGRES                            │
                │  - Find or create conversation       │
                │  - Insert user message               │
                │  - Load fiche logement + historique  │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  CLAUDE API (claude-sonnet-4-6)      │
                │  Génère brouillon (prompt caching)   │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  POSTGRES                            │
                │  Insert assistant draft (pending)    │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  DASHBOARD FLASK / (Basic Auth)      │
                │  Message + brouillon + boutons       │
                │  Envoyer / Modifier / Rejeter        │
                │  → < 30 sec de validation humaine    │
                └──────────────┬───────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────────┐
                │  SCRAPER PLAYWRIGHT (Phase 3)        │
                │  Login passwordless (code email) +   │
                │  envoi via UI Airbnb avec délais     │
                │  humains (2-8s entre actions)        │
                └──────────────────────────────────────┘
```

### Services Docker

| Service | Image | Port | Statut |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | 5432 (interne) | Toujours actif |
| `caddy` | `caddy:2.8` | 80, 443 | Toujours actif (TLS auto) |
| `dashboard` | build local | 8080 | Toujours actif |
| `scraper` | `mcr.microsoft.com/playwright/python` | — | Profile `worker` (activable à la demande) |
| `n8n` | `docker.n8n.io/n8nio/n8n:latest` | 5678 | Installé mais inutilisé (réservé V1) |
| `tests` | build local | — | Profile `tests` (CI / dev) |

---

## Démarrage

### Prérequis

- Docker + Docker Compose sur le serveur cible (Ubuntu 22.04 testé)
- Un nom de domaine pointant sur l'IP du VPS (recommandé : sous-domaine DuckDNS gratuit, `https://www.duckdns.org`)
- Une clé API Anthropic — `https://console.anthropic.com`
- Un compte Mailgun avec une **Inbound Route** configurée pour rediriger vers `https://[domaine]/webhook/email` — `https://app.mailgun.com`
- Une adresse email de compte hôte Airbnb (utilisée par le scraper, login passwordless)

### Configuration

```bash
cp .env.example .env
# Renseigne les variables (mots de passe, clés API, IP du VPS,
# signing key Mailgun, email du compte Airbnb)
```

Variables critiques :

| Variable | Description |
|---|---|
| `CLAUDE_API_KEY` | Clé Anthropic (`sk-ant-...`) |
| `MAILGUN_SIGNING_KEY` | Signing key Mailgun (Settings → Webhooks) |
| `WEBHOOK_SECRET` | Token aléatoire pour `/webhook/reception` (`openssl rand -hex 32`) |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | Basic Auth du dashboard |
| `AIRBNB_LOGIN_EMAIL` | Email du compte hôte (le mot de passe n'est jamais stocké, login via code email) |

### Caddy (TLS automatique)

Le fichier `caddy/Caddyfile` est pré-configuré : remplace `autohote-igor.duckdns.org` par ton domaine, ainsi que l'email pour Let's Encrypt. Caddy obtient et renouvelle automatiquement le certificat.

### Déploiement

```bash
# Démarre postgres + caddy + dashboard + n8n
docker compose up -d

# Initialise le schéma (idempotent — peut être relancé sans risque)
docker compose exec -T postgres psql -U autohote -d autohote -f /scripts/init_db.sql

# Active le worker scraper quand nécessaire (Phase 3+)
docker compose --profile worker up -d scraper
```

Accès :

- Dashboard de validation : `https://[domaine]/`
- Webhook réception emails Airbnb : `https://[domaine]/webhook/email` (Mailgun uniquement, signature HMAC)
- Webhook test/manuel : `https://[domaine]/webhook/reception` (header `X-Webhook-Secret`)

---

## Structure du projet

```
.
├── docker-compose.yml
├── .env.example
├── caddy/
│   └── Caddyfile               # Reverse proxy + Let's Encrypt
├── dashboard/                  # Service Flask
│   ├── Dockerfile
│   ├── app.py                  # Routes + webhook email + endpoints API
│   ├── jobs.py                 # File d'attente Postgres (scraper_jobs)
│   ├── requirements.txt
│   └── templates/
│       └── index.html
├── scraper/                    # Worker Playwright (profile "worker")
│   ├── Dockerfile              # Image Playwright officielle
│   ├── db.py                   # Connexion Postgres autonome
│   ├── session.py              # Persistance cookies (storage_state)
│   ├── selectors.py            # Sélecteurs CSS Airbnb centralisés
│   └── airbnb_auth.py          # Login passwordless (code email)
├── scripts/
│   ├── init_db.sql             # Schéma complet à jour
│   ├── migration_001_*.sql     # Migrations versionnées idempotentes
│   ├── migration_002_*.sql
│   ├── migration_003_*.sql
│   ├── migration_004_*.sql
│   ├── seed_test_message.py    # Génère un message de test + appel Claude
│   └── run_tests.sh            # Lanceur pytest
├── tests/                      # Suite pytest (~117 tests)
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│       └── airbnb_emails/      # Fixtures d'emails Airbnb anonymisés
└── workflows/                  # Exports JSON des workflows n8n (vide pour le moment)
```

---

## Base de données

Schéma principal (versionné dans `scripts/init_db.sql`) :

- **`sources`** — Couche d'abstraction multi-plateforme (Airbnb, Booking.com, Vrbo, direct)
- **`logements`** — Fiche structurée de chaque bien (règles, équipements, WiFi, codes d'accès, horaires)
- **`conversations`** — Fil de conversation avec un voyageur (rattaché à un logement)
- **`messages`** — Chaque message entrant et sortant + brouillon Claude (statut `pending`/`sent`/`rejected`)
- **`scraper_jobs`** — File d'attente persistante pour le worker scraper (`FOR UPDATE SKIP LOCKED`, retry exponentiel)
- **`airbnb_auth_codes`** — Codes de login Airbnb captés temporairement par Mailgun

Les migrations dans `scripts/migration_NNN_*.sql` sont **idempotentes** (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).

---

## Tests

```bash
./scripts/run_tests.sh                          # Suite complète
./scripts/run_tests.sh tests/unit               # Tests unitaires uniquement
./scripts/run_tests.sh tests/integration        # Tests d'intégration uniquement
./scripts/run_tests.sh -k "test_send"           # Filtre par nom
```

La suite tourne dans un conteneur Docker isolé contre une base `autohote_test` créée et détruite à chaque run. ~117 tests, 100% verts requis avant tout déploiement.

---

## Sécurité

- HTTPS obligatoire (Caddy + Let's Encrypt) — pas d'exposition HTTP en clair
- Signature HMAC vérifiée sur `/webhook/email` (Mailgun)
- Token partagé sur `/webhook/reception` (header `X-Webhook-Secret`)
- HTTP Basic Auth sur le dashboard
- Secrets en variables d'environnement uniquement, **jamais committés**
- Postgres non exposé sur internet (réseau Docker interne uniquement)

---

## Roadmap

- **MVP** (Semaines 1-4) — Infrastructure, base de données, génération Claude, dashboard de validation, réception emails Mailgun, worker scraper avec login Airbnb passwordless ✅ *Livré jusqu'à Phase 3a*
- **MVP Phases 3b-3d** — Lecture/écriture conversations Airbnb via Playwright, worker loop, intégration `/webhook/email` ↔ queue scraper ⏳ *En cours*
- **V1** — Séquences automatiques (J-2 pré-arrivée, J0 bienvenue, J+1 demande d'avis), tableau de bord conformité loi Le Meur, monitoring
- **V2** — Multi-plateforme (Booking.com, Vrbo), routage modèle (Haiku pour FAQ, Sonnet pour cas complexes), apprentissage par feedback humain sur les rejets

---

## Licence

Projet privé.
