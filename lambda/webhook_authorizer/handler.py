"""Webhook authorizer & ingest Lambda.

Sits behind API Gateway (HTTP API, payload format 2.0) and fronts the SQS
webhook queue. It fuses the architecture's "Lambda Authorizer (HMAC validation &
challenge response)" with the SQS enqueue step, because an HTTP API Lambda
*authorizer* cannot itself return Slack's synchronous URL-verification challenge
body - a proxy integration can.

Responsibilities:
  1. Verify the request signature (Shopify HMAC-SHA256 / Slack v0 signature).
  2. Answer Slack's ``url_verification`` challenge synchronously.
  3. Drop verified payloads onto SQS as a normalized envelope:
        {"source": "shopify"|"slack", "topic": "...", "payload": {...}}
  4. Reject anything unsigned/invalid with 401.

Environment:
  SQS_QUEUE_URL, SHOPIFY_WEBHOOK_SECRET, SLACK_SIGNING_SECRET, AWS_REGION
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qs

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sqs = boto3.client("sqs")
_secrets_client = boto3.client("secretsmanager")

SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
INTEGRATION_SECRET_ARN = os.environ.get("INTEGRATION_SECRET_ARN", "")
SLACK_MAX_SKEW = 300  # seconds

# Cached integration-secret JSON (populated once per warm container).
_secret_cache: dict | None = None


def _secret(key: str) -> str:
    """Return one key from the integration secret JSON (cached per container).

    Falls back to an equivalently named environment variable so the handler can
    run locally/in tests without Secrets Manager.
    """
    global _secret_cache
    if _secret_cache is None:
        if INTEGRATION_SECRET_ARN:
            try:
                resp = _secrets_client.get_secret_value(SecretId=INTEGRATION_SECRET_ARN)
                _secret_cache = json.loads(resp.get("SecretString") or "{}")
            except Exception:  # noqa: BLE001 - degrade to env fallback
                logger.exception(
                    "Failed to load integration secret; using env fallback."
                )
                _secret_cache = {}
        else:
            _secret_cache = {}
    return _secret_cache.get(key) or os.environ.get(key.upper(), "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _response(status: int, body=None, content_type="application/json"):
    payload = (
        body if isinstance(body, str) else json.dumps(body or {"ok": status < 400})
    )
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type},
        "body": payload,
    }


def _raw_body(event) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def _headers(event) -> dict:
    # HTTP API lowercases header names; normalise defensively anyway.
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _enqueue(envelope: dict) -> None:
    if not SQS_QUEUE_URL:
        raise RuntimeError("SQS_QUEUE_URL is not configured.")
    _sqs.send_message(QueueUrl=SQS_QUEUE_URL, MessageBody=json.dumps(envelope))


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------
def _verify_shopify(raw_body: str, provided_hmac: str) -> bool:
    secret = _secret("shopify_webhook_secret")
    if not secret or not provided_hmac:
        return False
    digest = hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, provided_hmac)


def _handle_shopify(event, headers, raw_body):
    if not _verify_shopify(raw_body, headers.get("x-shopify-hmac-sha256", "")):
        logger.warning("Rejected Shopify webhook: bad HMAC.")
        return _response(401, {"error": "invalid_signature"})
    try:
        payload = json.loads(raw_body or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid_json"})
    _enqueue(
        {
            "source": "shopify",
            "topic": headers.get("x-shopify-topic", "orders/risk"),
            "payload": payload,
        }
    )
    logger.info("Enqueued Shopify webhook topic=%s", headers.get("x-shopify-topic"))
    return _response(200, {"ok": True})


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def _verify_slack(headers, raw_body) -> bool:
    secret = _secret("slack_signing_secret")
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not secret or not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > SLACK_MAX_SKEW:
            return False
    except ValueError:
        return False
    basestring = f"v0:{ts}:{raw_body}".encode()
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", sig)


def _handle_slack(event, headers, raw_body):
    content_type = headers.get("content-type", "")
    parsed_json = None
    if content_type.startswith("application/json"):
        try:
            parsed_json = json.loads(raw_body or "{}")
        except json.JSONDecodeError:
            parsed_json = None
    if isinstance(parsed_json, dict) and parsed_json.get("type") == "url_verification":
        # Slack signs url_verification requests too, so verify whenever a
        # signing secret is configured. Only when no secret exists yet
        # (bootstrap/local runs) is the challenge answered unverified - it
        # merely echoes the challenge and never enqueues anything.
        if _secret("slack_signing_secret") and not _verify_slack(headers, raw_body):
            logger.warning("Rejected Slack url_verification: bad signature.")
            return _response(401, {"error": "invalid_signature"})
        return _response(200, {"challenge": parsed_json.get("challenge", "")})

    if not _verify_slack(headers, raw_body):
        logger.warning("Rejected Slack request: bad signature.")
        return _response(401, {"error": "invalid_signature"})

    # Interactive components arrive urlencoded as payload=<json>.
    if parsed_json is not None:
        interaction = parsed_json
    else:
        form = parse_qs(raw_body)
        payload_field = form.get("payload", ["{}"])[0]
        try:
            interaction = json.loads(payload_field)
        except json.JSONDecodeError:
            return _response(400, {"error": "invalid_payload"})

    _enqueue({"source": "slack", "topic": "interaction", "payload": interaction})
    logger.info(
        "Enqueued Slack interaction type=%s",
        interaction.get("type") if isinstance(interaction, dict) else "?",
    )
    # Slack expects a fast 200 to acknowledge the interaction.
    return _response(200, {"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _detect_source(event, headers) -> str:
    # Prefer the path parameter (/webhooks/{source}); fall back to headers.
    source = (event.get("pathParameters") or {}).get("source")
    if source:
        return source.lower()
    if "x-shopify-hmac-sha256" in headers:
        return "shopify"
    if "x-slack-signature" in headers or "x-slack-request-timestamp" in headers:
        return "slack"
    return "unknown"


def lambda_handler(event, context):
    headers = _headers(event)
    raw_body = _raw_body(event)
    source = _detect_source(event, headers)

    try:
        if source == "shopify":
            return _handle_shopify(event, headers, raw_body)
        if source == "slack":
            return _handle_slack(event, headers, raw_body)
        logger.warning("Unknown webhook source; headers=%s", list(headers.keys()))
        return _response(400, {"error": "unknown_source"})
    except Exception:  # noqa: BLE001 - never leak a stack trace to callers
        logger.exception("Webhook handling failed.")
        return _response(500, {"error": "internal_error"})
