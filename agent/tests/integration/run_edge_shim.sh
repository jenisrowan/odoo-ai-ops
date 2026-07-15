#!/usr/bin/env bash
# Start the DEV-ONLY edge shim (production HMAC Lambda's local stand-in).
#
#   ./agent/tests/integration/run_edge_shim.sh            # start (detached)
#   ./agent/tests/integration/run_edge_shim.sh logs       # follow logs
#   ./agent/tests/integration/run_edge_shim.sh stop
#
# Then point ngrok at it:   ngrok http 9100
# Shopify keeps posting to https://<your-ngrok-domain>/webhooks/shopify
#
# Captured raw deliveries land in agent/tests/integration/captures/.
set -euo pipefail
export MSYS_NO_PATHCONV=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

NAME="aiops-edge-shim"
NETWORK="${COMPOSE_NETWORK:-odoo-ai-ops_default}"
# 9000/9001 are MinIO's; 8000 is the agent's.
PORT="${SHIM_PORT:-9100}"
ODOO_DB="${ODOO_DB:-odoo_19}"
CAPTURES="$REPO_ROOT/agent/tests/integration/captures"

case "${1:-start}" in
  logs) exec docker logs -f "$NAME" ;;
  stop) docker rm -f "$NAME" >/dev/null 2>&1 || true; echo "shim stopped"; exit 0 ;;
esac

mkdir -p "$CAPTURES"
docker rm -f "$NAME" >/dev/null 2>&1 || true

# Docker Desktop on Windows wants //c/... style mount paths.
prefix() { case "$(uname -s)" in MINGW* | MSYS* | CYGWIN*) echo "/$1" ;; *) echo "$1" ;; esac; }

# --no-healthcheck: the agent image's HEALTHCHECK probes the agent's own port, so
# it would report this shim as "unhealthy" even while it serves fine.
docker run -d --name "$NAME" --network "$NETWORK" -p "${PORT}:9000" --env-file .env \
  --no-healthcheck \
  -e LANGFUSE_HOST=http://langfuse-web:3000 -e LANGFUSE_BASE_URL=http://langfuse-web:3000 \
  -e VALKEY_URL=redis://redis:6379 \
  -e ODOO_BASE_URL=http://web:8069 -e "ODOO_DB=$ODOO_DB" -e ODOO_USERNAME=admin -e ODOO_PASSWORD=admin \
  -e ENABLE_SQS_WORKER=false \
  -e AWS_DEFAULT_REGION=us-east-1 \
  -e LAMBDA_DIR=/lambda -e CAPTURE_DIR=/captures \
  -v "$(prefix "$REPO_ROOT/agent"):/app" \
  -v "$(prefix "$REPO_ROOT/lambda/webhook_authorizer"):/lambda:ro" \
  -v "$(prefix "$CAPTURES"):/captures" \
  -w /app \
  odoo-ai-ops-agent \
  python -m uvicorn tests.integration.edge_shim:app --host 0.0.0.0 --port 9000 >/dev/null

echo "edge shim starting on http://localhost:${PORT}  (captures -> $CAPTURES)"
echo "point ngrok at it:  ngrok http ${PORT}"
