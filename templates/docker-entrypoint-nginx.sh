#!/bin/sh
# =============================================================================
# Nginx Graceful Shutdown Entrypoint (Sidecar)
# =============================================================================
# This script is the entrypoint for the Nginx sidecar container. It wraps
# the stock nginx binary with a SIGTERM handler that triggers a graceful drain
# rather than an immediate (SIGKILL) stop.
#
# ECS Task Stopping Lifecycle (service-managed tasks):
#   DEACTIVATING — ECS tells the ALB to deregister this task and waits for
#                  the full deregistration_delay (60 s) to elapse. No new
#                  connections are routed to us during or after this phase.
#   STOPPING     — ECS then sends SIGTERM to all containers simultaneously.
#                  By this point the ALB has already finished draining, so
#                  it is safe to issue 'nginx -s quit' immediately.
#
# Shutdown sequence on SIGTERM:
#   1. 'nginx -s quit' — Nginx finishes any remaining keep-alive connections
#      that are still open (should be near zero after ALB draining) then exits.
#   2. Wait for nginx to exit cleanly.
#
# Note on manual 'aws ecs stop-task': this bypasses the DEACTIVATING phase,
# so SIGTERM arrives without prior ALB drain. For this setup (standard ECS
# service with managed scaling), manual stops should be rare and the brief
# window of potential 502s is an accepted trade-off versus the complexity of
# a cross-container sleep-then-quit mechanism.
# =============================================================================

set -eu

NGINX_PID=""

_graceful_shutdown() {
    echo "[nginx-entrypoint] SIGTERM received — issuing graceful drain (nginx -s quit)."

    if [ -z "$NGINX_PID" ]; then
        echo "[nginx-entrypoint] Nginx not yet started — exiting immediately."
        exit 0
    fi

    # 'nginx -s quit' sends SIGQUIT to the master, which:
    #   - Stops accepting new connections (health check will fail → ALB drains)
    #   - Waits for existing connections to close before workers exit
    nginx -s quit 2>/dev/null || true

    echo "[nginx-entrypoint] Waiting for nginx to finish draining..."
    wait "${NGINX_PID}" 2>/dev/null || true

    echo "[nginx-entrypoint] Nginx exited cleanly."
    exit 0
}

trap '_graceful_shutdown' TERM INT

echo "[nginx-entrypoint] Starting nginx..."
# Run nginx in the foreground as a background job so this script (PID 1)
# remains the signal target. '-g daemon off;' keeps nginx in the foreground.
nginx -g 'daemon off;' &
NGINX_PID=$!

echo "[nginx-entrypoint] Nginx started with PID ${NGINX_PID}. Waiting..."
wait "${NGINX_PID}"
EXIT_CODE=$?

echo "[nginx-entrypoint] Nginx exited with code ${EXIT_CODE}."
exit "${EXIT_CODE}"
