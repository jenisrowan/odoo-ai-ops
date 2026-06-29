#!/bin/bash
# =============================================================================
# Odoo Graceful Shutdown Entrypoint
# =============================================================================
# This script replaces the default Odoo entrypoint to support graceful shutdown
# during ECS scale-in events.
#
# ECS Task Stopping Lifecycle (service-managed tasks):
#   1. DEACTIVATING  — ECS instructs the ALB to deregister this target.
#                      The ALB stops routing NEW requests and waits for
#                      the deregistration_delay (60 s) for in-flight connections
#                      to complete. No traffic reaches this task after this phase.
#   2. STOPPING      — ECS sends SIGTERM to our containers. By this point,
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
    echo "[entrypoint] SIGTERM received — ECS has already drained the ALB. Stopping Odoo gracefully."

    if [ -z "$ODOO_PID" ]; then
        echo "[entrypoint] Odoo has not started yet — exiting immediately."
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
# Start Odoo via the upstream image entrypoint.
# We run it in the background so this script (PID 1) remains the signal target.
# The upstream /entrypoint.sh honours all env vars (HOST, PORT, USER, PASSWORD, …)
# and calls 'exec odoo' — but since we're backgrounding it, 'exec' just replaces
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
