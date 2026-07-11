#!/bin/bash
# =============================================================================
# Odoo Graceful Shutdown Entrypoint
# =============================================================================
# This script replaces the default Odoo entrypoint to support graceful shutdown
# during ECS scale-in events.
#
# ECS Task Stopping Lifecycle (service-managed tasks):
#   1. DEACTIVATING  - ECS instructs the ALB to deregister this target.
#                      The ALB stops routing NEW requests and waits for
#                      the deregistration_delay (60 s) for in-flight connections
#                      to complete. No traffic reaches this task after this phase.
#   2. STOPPING      - ECS sends SIGTERM to our containers. By this point,
#                      the ALB has already fully drained. No sleep needed.
#
# Our shutdown sequence on SIGTERM:
#   1. Send SIGTERM to the Odoo master process.
#      In multiprocessing mode, the master forwards SIGTERM to all workers,
#      which finish their current request iteration then exit cleanly.
#   2. Wait for Odoo to fully exit before this script exits.
#
# ECS stopTimeout must cover Odoo worker drain time only (~30 s typical).
# We keep it at 120 s as a conservative safety net for long-running cron jobs.
# =============================================================================

set -euo pipefail

ODOO_PID=""

_graceful_shutdown() {
    echo "[entrypoint] SIGTERM received - ECS has already drained the ALB. Stopping Odoo gracefully."

    if [ -z "$ODOO_PID" ]; then
        echo "[entrypoint] Odoo has not started yet - exiting immediately."
        exit 0
    fi

    # Send SIGTERM to the Odoo master process.
    # In multiprocessing mode, the master forwards SIGTERM to all worker and
    # cron processes, which finish their current request iteration then exit.
    # No sleep needed: ECS already completed ALB deregistration before sending
    # this SIGTERM (DEACTIVATING phase), so no new traffic is arriving.
    echo "[entrypoint] Sending SIGTERM to Odoo master (PID ${ODOO_PID})..."
    kill -TERM "${ODOO_PID}" 2>/dev/null || true

    # Wait for all workers and cron threads to finish and exit.
    echo "[entrypoint] Waiting for Odoo to finish..."
    wait "${ODOO_PID}" 2>/dev/null || true

    echo "[entrypoint] Odoo exited cleanly. Shutdown complete."
    exit 0
}

# Register the signal handler BEFORE starting Odoo so we never miss a signal.
trap '_graceful_shutdown' SIGTERM SIGINT

# ---------------------------------------------------------------------------
# Master (super-admin) password — FAIL CLOSED.
# Odoo's config default for admin_passwd is the well-known string 'admin', and
# the upstream entrypoint only maps the DB env vars (HOST/PORT/USER/PASSWORD) -
# it never consumes ODOO_ADMIN_PASSWD. A weak/default master password exposes
# the JSON-RPC 'db' service (create/drop/dump) even with list_db = False, so we
# REFUSE to boot rather than fall back to 'admin':
#   * value == 'admin'                  -> fatal (the insecure default).
#   * unset/empty and not ODOO_STAGE=dev -> fatal (prod must inject the secret).
#   * unset/empty and ODOO_STAGE=dev     -> allowed; local dev uses the
#                                           admin_passwd baked into dev.conf.
# In production ODOO_ADMIN_PASSWD is injected from the odoo/admin/password
# secret; a hard exit here is caught by the ECS deployment circuit breaker,
# which aborts + rolls back the deploy (no crash-loop). The value is written
# into odoo.conf as a salted pbkdf2 hash so no plaintext copy survives on disk.
# ---------------------------------------------------------------------------
if [ "${ODOO_ADMIN_PASSWD:-}" = "admin" ]; then
    echo "[entrypoint] FATAL: ODOO_ADMIN_PASSWD is set to Odoo's insecure default 'admin'." >&2
    echo "[entrypoint] Put a strong value in the odoo/admin/password secret. Refusing to start." >&2
    exit 1
elif [ -n "${ODOO_ADMIN_PASSWD:-}" ]; then
    echo "[entrypoint] Setting admin_passwd from ODOO_ADMIN_PASSWD (hashed)."
    python3 - <<'PYEOF'
import os
from passlib.context import CryptContext

conf = "/etc/odoo/odoo.conf"
hashed = CryptContext(schemes=["pbkdf2_sha512"]).hash(os.environ["ODOO_ADMIN_PASSWD"])
with open(conf) as fh:
    lines = fh.readlines()
lines = [line for line in lines if not line.strip().startswith("admin_passwd")]
for i, line in enumerate(lines):
    if line.strip() == "[options]":
        lines.insert(i + 1, f"admin_passwd = {hashed}\n")
        break
else:
    raise SystemExit("odoo.conf has no [options] section")
with open(conf, "w") as fh:
    fh.writelines(lines)
PYEOF
elif [ "${ODOO_STAGE:-}" = "dev" ]; then
    echo "[entrypoint] ODOO_STAGE=dev and ODOO_ADMIN_PASSWD unset: using the" \
         "config's admin_passwd as-is (LOCAL DEVELOPMENT ONLY)."
else
    echo "[entrypoint] FATAL: ODOO_ADMIN_PASSWD is not set. Refusing to start with" >&2
    echo "[entrypoint] Odoo's insecure 'admin' default master password. Ensure the" >&2
    echo "[entrypoint] odoo/admin/password secret is injected (set ODOO_STAGE=dev for" >&2
    echo "[entrypoint] local development)." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Start Odoo via the upstream image entrypoint.
# We run it in the background so this script (PID 1) remains the signal target.
# The upstream /entrypoint.sh honours all env vars (HOST, PORT, USER, PASSWORD, …)
# and calls 'exec odoo' - but since we're backgrounding it, 'exec' just replaces
# the subshell, which is fine; the resulting odoo process is what we wait on.
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting Odoo..."
/entrypoint.sh odoo &
ODOO_PID=$!

echo "[entrypoint] Odoo started with PID ${ODOO_PID}. Waiting..."
# Wait returns when Odoo exits naturally (e.g. on a normal restart) or after
# our signal handler calls 'wait' explicitly.
wait "${ODOO_PID}"
EXIT_CODE=$?

echo "[entrypoint] Odoo process exited with code ${EXIT_CODE}."
exit "${EXIT_CODE}"
