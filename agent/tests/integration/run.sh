#!/usr/bin/env bash
# Run the full-stack integration test against the running docker-compose stack.
# Usage (from anywhere):  ./agent/tests/integration/run.sh
#
# Requires: `docker compose up -d` healthy, the `odoo-ai-ops-agent` image built,
# and .env with UNQUOTED Langfuse keys.
set -euo pipefail

# Windows Git Bash: stop MSYS from mangling /-prefixed args into Windows paths.
export MSYS_NO_PATHCONV=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

NETWORK="${COMPOSE_NETWORK:-odoo-ai-ops_default}"
ODOO_DB="${ODOO_DB:-odoo_19}"

# Docker Desktop on Windows wants a //c/... style mount path.
MOUNT_SRC="$REPO_ROOT/agent"
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) MOUNT_SRC="/$MOUNT_SRC" ;;
esac

exec docker run --rm --network "$NETWORK" --env-file .env \
  -e RUN_INTEGRATION=1 \
  -e LANGFUSE_HOST=http://langfuse-web:3000 -e LANGFUSE_BASE_URL=http://langfuse-web:3000 \
  -e VALKEY_URL=redis://redis:6379 \
  -e ODOO_BASE_URL=http://web:8069 -e "ODOO_DB=$ODOO_DB" -e ODOO_USERNAME=admin -e ODOO_PASSWORD=admin \
  -e ENABLE_SQS_WORKER=false -e CLICKHOUSE_HTTP=http://clickhouse:8123 \
  -v "$MOUNT_SRC:/app" -w /app \
  odoo-ai-ops-agent \
  bash -c "python -m pip install -q pytest pytest-asyncio 2>/dev/null; python -m pytest tests/integration -v -s --no-header -p no:cacheprovider"
