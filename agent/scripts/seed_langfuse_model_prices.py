#!/usr/bin/env python3
"""Teach Langfuse what our Claude models cost.

Langfuse prices a generation by matching ``provided_model_name`` against the
``match_pattern`` of a row in its model table, then multiplying by the token
counts. The table it ships with only goes up to Claude 3.5, so nothing matches
``claude-sonnet-5`` or ``claude-haiku-4-5`` and every generation is scored at
$0 - token usage lands correctly, cost is silently zero. That makes the cost
side of the telemetry stack (and anything derived from it, like
``cost_analysis.txt``) unusable until these rows exist.

This is not just a local-dev gap: the self-hosted Langfuse in ECS ships the
same built-in table, so run this against prod too.

Idempotent - re-running updates the existing rows rather than duplicating them.

    LANGFUSE_HOST=http://langfuse-web:3000 \
    LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
    python seed_langfuse_model_prices.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

# Prices are USD per single token (Langfuse stores per-token, not per-million).
# Source: Anthropic published pricing per million tokens.
#
# Sonnet 5 is seeded at its STANDARD rate ($3/$15), not the introductory
# $2/$10 that runs until 2026-08-31. Langfuse enforces one row per model name
# per project - it has no date-banded pricing we can reach through this API -
# so a single rate has to serve both periods. Standard is the right one to
# keep: it is what the cost model should plan against, and until the intro
# period ends Langfuse simply over-states Sonnet spend by a third, which is the
# safe direction to be wrong in for a budget.
_PER_MILLION = 1_000_000

MODELS: list[dict] = [
    {
        "modelName": "claude-sonnet-5",
        "matchPattern": r"(?i)^(claude-sonnet-5)$",
        "unit": "TOKENS",
        "inputPrice": 3.00 / _PER_MILLION,
        "outputPrice": 15.00 / _PER_MILLION,
    },
    {
        # MODEL_MEDIUM is set to the dated id in docker-compose, so the pattern
        # has to match both the alias and the pinned snapshot.
        "modelName": "claude-haiku-4-5",
        "matchPattern": r"(?i)^(claude-haiku-4-5|claude-haiku-4-5-\d{8})$",
        "unit": "TOKENS",
        "inputPrice": 1.00 / _PER_MILLION,
        "outputPrice": 5.00 / _PER_MILLION,
    },
]


def _auth_header() -> str:
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not (public and secret):
        sys.exit("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set.")
    return "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()


def _call(host: str, path: str, *, method: str = "GET", payload: dict | None = None):
    request = urllib.request.Request(
        f"{host.rstrip('/')}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else {}


def _drop_existing(host: str, model_name: str) -> None:
    """Remove any prior row for this model so re-runs update rather than 409.

    Langfuse keys models by name per project and rejects a duplicate, so an
    idempotent seed means delete-then-create. Only rows we manage are touched;
    Langfuse's own built-in entries are read-only and never match these names.
    """
    page = 1
    while True:
        result = _call(host, f"/api/public/models?page={page}&limit=100")
        rows = result.get("data") or []
        if not rows:
            return
        for row in rows:
            if row.get("modelName") == model_name and not row.get("isLangfuseManaged"):
                _call(host, f"/api/public/models/{row['id']}", method="DELETE")
                print(f"  removed old row for {model_name} [id={row['id']}]")
        if page >= (result.get("meta") or {}).get("totalPages", 1):
            return
        page += 1


def main() -> int:
    host = os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")
    if not host:
        sys.exit("LANGFUSE_HOST (or LANGFUSE_BASE_URL) must be set.")

    failures = 0
    for model in MODELS:
        name = model["modelName"]
        try:
            _drop_existing(host, name)
            created = _call(host, "/api/public/models", method="POST", payload=model)
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - report each row
            failures += 1
            print(f"  FAILED  {name}: HTTP {exc.code} {exc.read().decode()[:200]}")
        else:
            print(
                f"  ok      {name}: "
                f"${model['inputPrice'] * _PER_MILLION:.2f} in / "
                f"${model['outputPrice'] * _PER_MILLION:.2f} out per 1M  "
                f"[id={created.get('id')}]"
            )

    if failures:
        print(f"\n{failures} of {len(MODELS)} model prices failed to seed.")
        return 1
    print(
        f"\nSeeded {len(MODELS)} model prices. Generations recorded from now on "
        "carry a cost; traces already in ClickHouse keep their $0."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
