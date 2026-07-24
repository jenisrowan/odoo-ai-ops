"""Live integration tests against the real Shopify store.

These hit the Shopify Admin API using the credentials in the environment / repo
``.env`` (``SHOPIFY_SHOP_DOMAIN``, ``SHOPIFY_ADMIN_TOKEN``). They are **gated**:
nothing runs unless ``RUN_SHOPIFY_LIVE_TESTS=1`` AND the credentials are present,
so a normal ``pytest`` / CI run skips the whole module.

Run:
    RUN_SHOPIFY_LIVE_TESTS=1 pytest custom_addons/odoo_ai_ops/tests/live -v

Safety:
* The connectivity / list / inventory-read tests are read-only.
* ``test_register_and_verify_webhooks`` WRITES: it registers HTTPS webhook
  subscriptions on the live store pointing at the ngrok callback URL. It is
  idempotent (re-running changes nothing) and is the point of the exercise.
* The order-cancel and inventory-write tests are DESTRUCTIVE and stay skipped
  unless you explicitly point them at a throwaway target via
  ``SHOPIFY_LIVE_TEST_ORDER_ID`` / ``SHOPIFY_LIVE_TEST_SKU``.
"""

from __future__ import annotations

import base64
import json
import os

import pytest

_RUN = os.environ.get("RUN_SHOPIFY_LIVE_TESTS") == "1"
_HAVE_CREDS = bool(os.environ.get("SHOPIFY_SHOP_DOMAIN") and os.environ.get("SHOPIFY_ADMIN_TOKEN"))

pytestmark = pytest.mark.skipif(
    not (_RUN and _HAVE_CREDS),
    reason="live Shopify tests: set RUN_SHOPIFY_LIVE_TESTS=1 and Shopify creds (env / .env)",
)


def _normalize_domain(dom: str) -> str:
    return (dom or "").lower().replace("https://", "").replace("http://", "").strip("/")


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------
def test_credentials_and_connectivity(client):
    """The admin token authenticates and resolves the configured store."""
    shop = client.get_shop_info()
    assert shop.get("name"), f"empty shop info (bad token?): {shop!r}"
    configured = _normalize_domain(os.environ["SHOPIFY_SHOP_DOMAIN"])
    returned = _normalize_domain(shop.get("myshopifyDomain", ""))
    # The token must belong to the store we think we're targeting.
    assert returned == configured, f"token store {returned!r} != configured {configured!r}"


def test_list_webhooks_is_readable(client):
    subs = client.list_webhooks()
    assert isinstance(subs, list)
    for sub in subs:
        assert {"id", "topic", "callback_url"} <= set(sub)


def test_inventory_read_best_effort(client):
    """Read available inventory for a real SKU if the store has one."""
    sku = _first_sku(client)
    if not sku:
        pytest.skip("store has no product variant with a SKU")
    qty = client.get_available_inventory(sku)
    assert qty is None or isinstance(qty, float)


# ---------------------------------------------------------------------------
# Write (authorized): register the pipeline's webhooks to the ngrok URL
# ---------------------------------------------------------------------------
def test_register_and_verify_webhooks(client, callback_url, shopify_mod):
    summary = client.sync_webhooks(callback_url)
    # Every ingested topic must now have a subscription pointing at our URL.
    subs = client.list_webhooks()
    live_topics = {s["topic"] for s in subs if s.get("callback_url") == callback_url}
    for topic_enum in shopify_mod.WEBHOOK_TOPICS:
        assert topic_enum in live_topics, f"{topic_enum} not registered to {callback_url}; sync={summary}; subs={subs}"
    # Idempotency: a second sync must neither create nor update anything.
    again = client.sync_webhooks(callback_url)
    assert not again["created"] and not again["updated"], f"second sync not idempotent: {again}"


def test_webhook_secret_verifies_real_shopify_signatures(captures_dir, lambda_handler_mod):
    """The configured secret is the one Shopify actually signs with.

    Every other HMAC test in the repo signs a body with a secret and verifies it
    with the same secret. That proves the algorithm, but passes no matter what
    ``SHOPIFY_WEBHOOK_SECRET`` is set to. The only way to prove the *value* is to
    check it against something Shopify signed itself.

    That is what the edge shim's captures give us: each one stores the raw body
    Shopify sent (``raw_b64``) next to the ``x-shopify-hmac-sha256`` header it
    sent with it. We replay them through the production Lambda verifier.

    If this fails, the Lambda 401s every genuine delivery, the pipeline silently
    drops all orders, and no other test in the repo notices. For an Admin-API
    created subscription the secret is the custom app's API secret key.
    """
    deliveries = _shopify_captures(captures_dir)
    if not deliveries:
        pytest.skip(
            f"no captured Shopify deliveries in {captures_dir} - run "
            "agent/tests/integration/run_edge_shim.sh and let Shopify deliver one"
        )

    for path, raw_body, signature in deliveries:
        assert lambda_handler_mod._verify_shopify(raw_body, signature), (
            f"SHOPIFY_WEBHOOK_SECRET does not match what Shopify signed {path.name} "
            f"with - every live delivery would be rejected with 401"
        )

    # A real delivery with one byte added must fail, so the check above cannot be
    # passing simply because the verifier accepts everything.
    path, raw_body, signature = deliveries[0]
    assert not lambda_handler_mod._verify_shopify(raw_body + " ", signature)


# ---------------------------------------------------------------------------
# Destructive — opt-in only (skipped unless a throwaway target is provided)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.environ.get("SHOPIFY_LIVE_TEST_ORDER_ID"),
    reason="DESTRUCTIVE: set SHOPIFY_LIVE_TEST_ORDER_ID (a throwaway order) to run",
)
def test_cancel_order_destructive(client):
    order_id = os.environ["SHOPIFY_LIVE_TEST_ORDER_ID"]
    job = client.cancel_order(order_id, reason="STAFF", staff_note="AI Ops live test")
    assert isinstance(job, dict)


@pytest.mark.skipif(
    not os.environ.get("SHOPIFY_LIVE_TEST_SKU"),
    reason="DESTRUCTIVE: set SHOPIFY_LIVE_TEST_SKU (a throwaway SKU) to run",
)
def test_set_inventory_destructive(client):
    sku = os.environ["SHOPIFY_LIVE_TEST_SKU"]
    before = client.get_available_inventory(sku)
    assert before is not None, f"SKU {sku} has no stocked inventory to adjust"
    target = int(before) + 1
    client.set_inventory_quantity(sku, target, reason="correction")
    after = client.get_available_inventory(sku)
    assert after == float(target)
    # Restore the original level so the test leaves no trace.
    client.set_inventory_quantity(sku, int(before), reason="correction")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_PRODUCTS_QUERY = """
query FirstSkus {
  products(first: 10) {
    edges { node { variants(first: 10) { edges { node { sku } } } } }
  }
}
"""


def _first_sku(client):
    data = client._execute(_PRODUCTS_QUERY, {})
    for p_edge in ((data or {}).get("products") or {}).get("edges", []):
        for v_edge in (p_edge["node"].get("variants") or {}).get("edges", []):
            sku = (v_edge["node"] or {}).get("sku")
            if sku:
                return sku
    return None


def _shopify_captures(captures_dir):
    """Captured deliveries that carry a Shopify signature, as (path, raw, sig).

    ``raw_b64`` holds the exact bytes Shopify signed. The parsed ``payload`` in
    the same file cannot be used - re-serialising it changes the bytes (spacing,
    escaped slashes) and would fail verification for reasons unrelated to the
    secret. Slack captures are skipped; they have no Shopify header.
    """
    if not captures_dir.is_dir():
        return []
    found = []
    for path in sorted(captures_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        signature = (data.get("headers") or {}).get("x-shopify-hmac-sha256")
        if data.get("raw_b64") and signature:
            found.append((path, base64.b64decode(data["raw_b64"]).decode("utf-8"), signature))
    return found
