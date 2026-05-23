#!/usr/bin/env bash
# Lance la suite de tests AutoHôte (unit + integration) dans un conteneur dédié.
#
# Usage :
#   ./scripts/run_tests.sh                # tous les tests
#   ./scripts/run_tests.sh tests/unit     # un sous-dossier
#   ./scripts/run_tests.sh -k "test_send" # filtre pytest
#
# Le script sync les fichiers vers le VPS, builde l'image tests si nécessaire,
# et lance pytest contre une BDD isolée `autohote_test` (créée/dropped à chaque run).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VPS_ALIAS="autohote"
REMOTE_PATH="~/autohote"

echo "📤 Sync vers le VPS..."
rsync -avq \
    --delete \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.git' \
    --exclude='.DS_Store' \
    --exclude='.env' \
    "${PROJECT_ROOT}/tests/" "${VPS_ALIAS}:${REMOTE_PATH}/tests/"

rsync -avq \
    --exclude='__pycache__' \
    "${PROJECT_ROOT}/scripts/" "${VPS_ALIAS}:${REMOTE_PATH}/scripts/"

rsync -avq \
    --exclude='__pycache__' \
    "${PROJECT_ROOT}/dashboard/" "${VPS_ALIAS}:${REMOTE_PATH}/dashboard/"

# docker-compose.yml uniquement (le .env contient des secrets, on ne le sync que si on en a vraiment besoin)
scp -q "${PROJECT_ROOT}/docker-compose.yml" "${VPS_ALIAS}:${REMOTE_PATH}/docker-compose.yml"

echo "🧪 Exécution des tests..."
# On passe les arguments éventuels (pytest -k, chemin de test...) au container
ssh "${VPS_ALIAS}" "cd ${REMOTE_PATH} && docker compose --profile tests run --rm --build tests pytest -v --tb=short /app/tests $*"
