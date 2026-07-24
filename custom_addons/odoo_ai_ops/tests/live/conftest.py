"""Pytest fixtures for the live Shopify tests.

Loads the repo-root ``.env`` (so a bare ``pytest`` run picks up credentials),
imports the ``ShopifyClient`` directly by file path (no Odoo runtime needed), and
exposes a configured client + the webhook callback URL.

These fixtures never print secret values.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest

# custom_addons/odoo_ai_ops/tests/live/conftest.py -> repo root is parents[4]
_HERE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[4]
_CLIENT_PATH = _HERE.parents[2] / "services" / "shopify_client.py"

# The HMAC test verifies real deliveries with the *production* Lambda code, so
# the handler package has to be importable. It builds boto3 clients at import,
# which needs a region but no credentials.
_LAMBDA_DIR = _REPO_ROOT / "lambda" / "webhook_authorizer"
_CAPTURES_DIR = _REPO_ROOT / "agent" / "tests" / "integration" / "captures"

# Default local webhook target (an ngrok tunnel). Override with
# SHOPIFY_WEBHOOK_CALLBACK_URL. Path mirrors the production edge route
# (API Gateway `POST /webhooks/{source}`).
_DEFAULT_NGROK_BASE = "https://barterer-dusk-retold.ngrok-free.dev"


def _load_dotenv(path: pathlib.Path) -> None:
    """Minimal .env loader (no python-dotenv dependency); never overrides real env."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(_REPO_ROOT / ".env")


def _load_client_module():
    spec = importlib.util.spec_from_file_location("odoo_ai_ops_shopify_client_live", _CLIENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def shopify_mod():
    return _load_client_module()


@pytest.fixture(scope="session")
def client(shopify_mod):
    return shopify_mod.ShopifyClient(
        shop_domain=os.environ["SHOPIFY_SHOP_DOMAIN"],
        admin_token=os.environ["SHOPIFY_ADMIN_TOKEN"],
        api_version=os.environ.get("SHOPIFY_API_VERSION", "2026-07"),
    )


@pytest.fixture(scope="session")
def captures_dir() -> pathlib.Path:
    """Where the edge shim writes raw Shopify deliveries."""
    return _CAPTURES_DIR


@pytest.fixture(scope="session")
def lambda_handler_mod():
    """The production ingest Lambda, imported with the real webhook secret in play."""
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("SQS_QUEUE_URL", "https://sqs.test.local/queue")
    if str(_LAMBDA_DIR) not in sys.path:
        sys.path.insert(0, str(_LAMBDA_DIR))
    import handler  # noqa: PLC0415  (path must be set up first)

    # _secret() caches per container and falls back to env; .env is already loaded.
    handler._secret_cache = None
    return handler


@pytest.fixture(scope="session")
def callback_url():
    explicit = os.environ.get("SHOPIFY_WEBHOOK_CALLBACK_URL")
    if explicit:
        return explicit
    base = os.environ.get("SHOPIFY_NGROK_BASE", _DEFAULT_NGROK_BASE).rstrip("/")
    return f"{base}/webhooks/shopify"
