"""Unit tests for the webhook authorizer/ingest Lambda.

Covers the security boundary (Shopify HMAC + Slack v0 signature), the Slack
url_verification challenge, SQS enqueue envelopes, base64 bodies, and rejection
paths. SQS is mocked; nothing hits AWS.
"""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest

import handler


@pytest.fixture(autouse=True)
def mock_sqs(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(handler, "_sqs", m)
    monkeypatch.setattr(handler, "_secret_cache", None)  # force env fallback
    return m


# A realistic `orders/create` body, byte-shaped the way Shopify actually sends
# one: compact separators (no space after ':' or ','), escaped forward slashes in
# the admin_graphql_api_id, and nested objects. Values are invented - only the
# byte layout needs to be genuine, because that is all HMAC sees.
#
# Signing a hand-built dict via json.dumps instead would produce bytes Shopify
# never emits (json.dumps adds spaces and leaves '/' unescaped), so the verifier
# would only ever be tested against a shape it will not meet in production.
SHOPIFY_RAW_BODY = (
    '{"id":6937917718659,"admin_graphql_api_id":"gid:\\/\\/shopify\\/Order\\/6937917718659",'
    '"app_id":396934447105,"currency":"USD","total_price":"250.00","email":"buyer@example.com",'
    '"customer":{"id":8123456789,"first_name":"Ada","last_name":"Lovelace",'
    '"email":"buyer@example.com"},'
    '"line_items":[{"id":15987654321,"title":"Widget","sku":"SKU-1","quantity":1,'
    '"price":"250.00"}],"test":true}'
)


def _shopify_sig(body, secret="shpsecret"):
    return base64.b64encode(
        hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    ).decode()


def _slack_sig(body, ts, secret="slacksecret"):
    digest = hmac.new(
        secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


# --- Shopify ---------------------------------------------------------------
def test_shopify_valid_hmac_enqueues(mock_sqs):
    body = SHOPIFY_RAW_BODY
    event = {
        "pathParameters": {"source": "shopify"},
        "headers": {
            "x-shopify-hmac-sha256": _shopify_sig(body),
            "x-shopify-topic": "orders/create",
        },
        "body": body,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 200
    mock_sqs.send_message.assert_called_once()
    sent = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
    assert sent["source"] == "shopify"
    assert sent["topic"] == "orders/create"
    assert sent["payload"]["id"] == 6937917718659
    assert sent["payload"]["line_items"][0]["sku"] == "SKU-1"


def test_shopify_reserialized_body_fails_hmac(mock_sqs):
    """Verification is byte-exact, not JSON-equal.

    Re-encoding the same payload with ``json.dumps`` yields semantically
    identical JSON with different bytes, and must not verify. This is the trap
    behind "the secret looks wrong but isn't": always hash what arrived on the
    wire, never a re-serialised parse of it.
    """
    reserialized = json.dumps(json.loads(SHOPIFY_RAW_BODY))
    assert reserialized != SHOPIFY_RAW_BODY  # spacing + '/' escaping differ
    event = {
        "pathParameters": {"source": "shopify"},
        "headers": {"x-shopify-hmac-sha256": _shopify_sig(SHOPIFY_RAW_BODY)},
        "body": reserialized,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 401
    mock_sqs.send_message.assert_not_called()


def test_shopify_bad_hmac_rejected(mock_sqs):
    body = json.dumps({"id": 1})
    event = {
        "pathParameters": {"source": "shopify"},
        "headers": {"x-shopify-hmac-sha256": "not-the-right-signature"},
        "body": body,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 401
    mock_sqs.send_message.assert_not_called()


def test_base64_encoded_body(mock_sqs):
    body = SHOPIFY_RAW_BODY
    event = {
        "pathParameters": {"source": "shopify"},
        "headers": {"x-shopify-hmac-sha256": _shopify_sig(body)},
        "body": base64.b64encode(body.encode()).decode(),
        "isBase64Encoded": True,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 200
    mock_sqs.send_message.assert_called_once()


# --- Slack -----------------------------------------------------------------
def test_slack_url_verification_challenge_signed(mock_sqs):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"})
    ts = str(int(time.time()))
    event = {
        "pathParameters": {"source": "slack"},
        "headers": {
            "content-type": "application/json",
            "x-slack-request-timestamp": ts,
            "x-slack-signature": _slack_sig(body, ts),
        },
        "body": body,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["challenge"] == "abc123"
    mock_sqs.send_message.assert_not_called()


def test_slack_url_verification_unsigned_rejected_when_secret_set(mock_sqs):
    # Slack signs url_verification requests; with a signing secret configured
    # an unsigned challenge must be rejected (no unauthenticated echo).
    body = json.dumps({"type": "url_verification", "challenge": "abc123"})
    event = {
        "pathParameters": {"source": "slack"},
        "headers": {"content-type": "application/json"},
        "body": body,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 401
    mock_sqs.send_message.assert_not_called()


def test_slack_interaction_valid_signature_enqueues(mock_sqs):
    interaction = {
        "type": "block_actions",
        "actions": [{"action_id": "ai_ops_reject", "value": "{}"}],
    }
    body = "payload=" + quote(json.dumps(interaction))
    ts = str(int(time.time()))
    event = {
        "pathParameters": {"source": "slack"},
        "headers": {
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": ts,
            "x-slack-signature": _slack_sig(body, ts),
        },
        "body": body,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 200
    mock_sqs.send_message.assert_called_once()
    sent = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
    assert sent["source"] == "slack"
    assert sent["payload"]["type"] == "block_actions"


def test_slack_bad_signature_rejected(mock_sqs):
    body = "payload=%7B%7D"
    ts = str(int(time.time()))
    event = {
        "pathParameters": {"source": "slack"},
        "headers": {
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": ts,
            "x-slack-signature": "v0=deadbeef",
        },
        "body": body,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 401
    mock_sqs.send_message.assert_not_called()


def test_slack_stale_timestamp_rejected(mock_sqs):
    body = "payload=%7B%7D"
    ts = str(int(time.time()) - 3600)  # an hour old -> replay
    event = {
        "pathParameters": {"source": "slack"},
        "headers": {
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": ts,
            "x-slack-signature": _slack_sig(body, ts),
        },
        "body": body,
    }
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 401


# --- Routing ---------------------------------------------------------------
def test_unknown_source_rejected(mock_sqs):
    resp = handler.lambda_handler({"headers": {}, "body": ""}, None)
    assert resp["statusCode"] == 400
    mock_sqs.send_message.assert_not_called()


def test_source_detected_from_headers_when_no_path_param(mock_sqs):
    body = SHOPIFY_RAW_BODY
    event = {"headers": {"x-shopify-hmac-sha256": _shopify_sig(body)}, "body": body}
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 200
