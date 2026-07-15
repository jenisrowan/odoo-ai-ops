"""DEV-ONLY local edge shim - the production HMAC Lambda's stand-in.

Production:  Shopify -> CloudFront -> API GW -> Lambda (HMAC) -> SQS -> agent -> Odoo
Locally:     Shopify -> ngrok -> THIS SHIM (HMAC) ------------> agent -> Odoo

There is no Lambda/API-GW/SQS locally and we don't want them: the shim does the
Lambda's job and hands the *same envelope* straight to the agent's real routing
(`AgentRuntime.handle_sqs_message`), which forwards to Odoo over the shared token
exactly as in production. Odoo never sees an HMAC - that stays at the edge.

It deliberately **imports the real Lambda's `_verify_shopify`** rather than
re-implementing it, so the shim can never drift from the code that runs in prod.

It also dumps every delivery (headers + raw body) to CAPTURE_DIR, which is how we
learn the exact payload Shopify really sends for a topic (e.g. the undocumented
`orders/risk_assessment_changed` shape).

This is test scaffolding: it lives under tests/, never in the `app` package.
Run it with:  agent/tests/integration/run_edge_shim.sh
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import pathlib
import sys
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Response

from app.config import Settings
from app.runtime import AgentRuntime

# The real Lambda handler is mounted at /lambda (see run_edge_shim.sh). Importing
# it needs AWS_DEFAULT_REGION (it builds boto3 clients at import) but no creds:
# with INTEGRATION_SECRET_ARN unset, `_secret()` falls back to env vars.
sys.path.insert(0, os.environ.get("LAMBDA_DIR", "/lambda"))
from handler import _verify_shopify, _verify_slack  # noqa: E402  (production signature checks)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [shim] %(message)s")
logger = logging.getLogger("edge_shim")

CAPTURE_DIR = pathlib.Path(os.environ.get("CAPTURE_DIR", "/captures"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.runtime = await AgentRuntime.create(Settings())
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("edge shim ready; captures -> %s", CAPTURE_DIR)
    yield
    await app.state.runtime.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "edge_shim"}


def _capture(topic: str, headers: dict, raw: bytes, verified: bool) -> pathlib.Path:
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S_%f")
    name = f"{ts}-{(topic or 'unknown').replace('/', '_')}{'' if verified else '-UNVERIFIED'}.json"
    path = CAPTURE_DIR / name
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {"_raw": raw.decode("utf-8", "replace")}
    path.write_text(
        json.dumps(
            {
                "topic": topic,
                "verified": verified,
                "headers": headers,
                "payload": body,
                # Exact bytes, so a signature can be re-checked against another
                # candidate secret without needing a fresh delivery.
                "raw_b64": base64.b64encode(raw).decode(),
            },
            indent=2,
        )
    )
    return path


@app.post("/webhooks/shopify")
async def shopify_webhook(request: Request):
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    topic = headers.get("x-shopify-topic", "")
    sig = headers.get("x-shopify-hmac-sha256", "")

    # Same check the production Lambda performs, using its own code.
    verified = _verify_shopify(raw.decode("utf-8", "replace"), sig)
    path = _capture(topic, headers, raw, verified)
    logger.info("delivery topic=%r verified=%s captured=%s", topic, verified, path.name)

    if not verified:
        # A forged/test request failing here is fine. A *genuine* Shopify
        # delivery failing means SHOPIFY_WEBHOOK_SECRET is not the secret Shopify
        # signs with (for Admin-API-created subscriptions that is the app's
        # client secret / "API secret key") - in production the Lambda would then
        # 401 every real webhook and the pipeline would silently drop all orders.
        if "shopify" in headers.get("user-agent", "").lower():
            logger.error(
                "REAL Shopify delivery FAILED HMAC (topic=%r, webhook-id=%s). "
                "SHOPIFY_WEBHOOK_SECRET is almost certainly wrong - it must be the "
                "custom app's API secret key. Capture: %s",
                topic,
                headers.get("x-shopify-webhook-id"),
                path.name,
            )
        return Response(
            json.dumps({"error": "invalid_signature"}),
            status_code=401,
            media_type="application/json",
        )

    payload = json.loads(raw or b"{}")
    # The exact envelope the Lambda enqueues to SQS; handed straight to the agent.
    envelope = {"source": "shopify", "topic": topic, "payload": payload}
    try:
        await request.app.state.runtime.handle_sqs_message(envelope)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the shim
        logger.exception("agent routing failed for topic %r", topic)
        return Response(
            json.dumps({"error": "processing_failed", "detail": str(exc)}),
            status_code=500,
            media_type="application/json",
        )
    return {"ok": True, "topic": topic}


@app.post("/webhooks/slack")
async def slack_interaction(request: Request):
    """Slack's interactivity endpoint (Approve/Reject clicks).

    Mirrors the Lambda's `_handle_slack`: verify the v0 signature, answer the
    url_verification challenge, then hand the interaction to the agent's real
    routing, which resumes the paused workflow from Valkey and writes the
    decision back to Odoo.

    Point the Slack app's *Interactivity Request URL* at
    https://<ngrok-domain>/webhooks/slack
    """
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    body_str = raw.decode("utf-8", "replace")

    parsed_json = None
    if headers.get("content-type", "").startswith("application/json"):
        try:
            parsed_json = json.loads(body_str or "{}")
        except ValueError:
            parsed_json = None

    # Slack signs the url_verification handshake too.
    if isinstance(parsed_json, dict) and parsed_json.get("type") == "url_verification":
        if os.environ.get("SLACK_SIGNING_SECRET") and not _verify_slack(headers, body_str):
            return Response(
                json.dumps({"error": "invalid_signature"}),
                status_code=401,
                media_type="application/json",
            )
        return {"challenge": parsed_json.get("challenge", "")}

    if not _verify_slack(headers, body_str):
        _capture("slack/interaction", headers, raw, False)
        logger.error(
            "Slack interaction FAILED the v0 signature check - is SLACK_SIGNING_SECRET set "
            "(Slack app -> Basic Information -> Signing Secret)?"
        )
        return Response(
            json.dumps({"error": "invalid_signature"}),
            status_code=401,
            media_type="application/json",
        )

    # Interactive components arrive urlencoded as payload=<json>.
    if parsed_json is not None:
        interaction = parsed_json
    else:
        try:
            interaction = json.loads(parse_qs(body_str).get("payload", ["{}"])[0])
        except ValueError:
            return Response(
                json.dumps({"error": "invalid_payload"}),
                status_code=400,
                media_type="application/json",
            )

    path = _capture("slack/interaction", headers, raw, True)
    logger.info("slack interaction type=%s captured=%s", interaction.get("type"), path.name)
    try:
        await request.app.state.runtime.handle_sqs_message(
            {"source": "slack", "topic": "interaction", "payload": interaction}
        )
    except Exception as exc:  # noqa: BLE001 - Slack needs a fast, non-crashing reply
        logger.exception("agent routing failed for the Slack interaction")
        return Response(
            json.dumps({"error": "processing_failed", "detail": str(exc)}),
            status_code=500,
            media_type="application/json",
        )
    return {"ok": True}
