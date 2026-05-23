# AutoHôte

Outil d'automatisation des conversations Airbnb avec validation humaine.

Réduit le temps de réponse aux voyageurs de ~20 min à ~30 sec par message en générant un brouillon de réponse à partir d'une fiche logement structurée et de l'historique de la conversation. Chaque réponse passe par une validation humaine via un dashboard web avant envoi.

---

## Stack

| Composant | Technologie |
|---|---|
| Conteneurisation | Docker + Docker Compose |
| Orchestration workflows | n8n (self-hosted) |
| Dashboard validation | Flask + Gunicorn |
| Base de données | PostgreSQL 16 |
| Génération de réponses | Anthropic Claude API |
| Scraping Airbnb | Apify (proxies résidentiels) |
| Hébergement cible | VPS Ubuntu 22.04 |

---

## Architecture

```
[AIRBNB]                              [DASHBOARD]
   │ message entrant                       ▲ validation humaine
   ▼                                       │
[APIFY scrape ──webhook──▶ n8n WORKFLOW]   │
                              │            │
                              ▼            │
                       [PostgreSQL] ◀──────┘
                              │
                              ▼
                       [Claude API]
                              │
                              ▼
                       [n8n → DASHBOARD]
                              │
                              ▼ < 30 sec
                       [APIFY ──▶ Airbnb]
```

3 services Docker tournent sur le VPS :

- **`postgres`** — base applicative (logements, conversations, messages)
- **`n8n`** — orchestration des workflows (port 5678)
- **`dashboard`** — interface de validation Flask (port 8080, HTTP Basic Auth)

---

## Démarrage

### Prérequis

- Docker + Docker Compose installés sur le serveur cible
- Une clé API Anthropic (`console.anthropic.com`)
- Un compte Apify avec token API

### Configuration

```bash
cp .env.example .env
# Renseigner les variables (mots de passe, clés API, IP du serveur)
```

### Déploiement

```bash
docker compose up -d
```

Les trois services démarrent. Accès :

- Dashboard de validation : `http://[IP_SERVEUR]:8080`
- Interface n8n : `http://[IP_SERVEUR]:5678`

### Initialisation de la base

```bash
docker compose exec postgres psql -U autohote -d autohote -f /scripts/init_db.sql
```

(Le script `scripts/init_db.sql` est idempotent et peut être relancé sans risque.)

---

## Structure du projet

```
.
├── docker-compose.yml
├── .env.example
├── dashboard/                 # Service Flask de validation
│   ├── Dockerfile
│   ├── app.py
│   ├── requirements.txt
│   └── templates/
├── scripts/
│   ├── init_db.sql            # Schéma PostgreSQL initial
│   ├── seed_test_message.py   # Génération d'un message de test
│   └── run_tests.sh           # Lanceur de la suite pytest
├── tests/                     # Tests unitaires et d'intégration
│   ├── unit/
│   └── integration/
└── workflows/                 # Exports JSON des workflows n8n
```

---

## Tests

```bash
./scripts/run_tests.sh                          # Suite complète
./scripts/run_tests.sh tests/unit               # Tests unitaires uniquement
./scripts/run_tests.sh -k "test_send"           # Filtre par nom
```

La suite tourne dans un conteneur Docker isolé contre une base `autohote_test` créée et détruite à chaque run.

---

## Roadmap

- **MVP** (en cours) — Infrastructure, base de données, génération Claude, dashboard de validation
- **V1** — Intégration Apify, séquences automatiques (J-2, J0, J+1), HTTPS via reverse proxy
- **V2** — Multi-plateforme (Booking.com, Vrbo), routage modèle (Haiku pour FAQ, Sonnet pour cas complexes)

---

## Licence

Projet privé.
