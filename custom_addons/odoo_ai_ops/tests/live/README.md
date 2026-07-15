# Live Shopify tests

Integration tests that hit the **real** Shopify Admin API. They are separate from
the mocked unit tests in `../test_shopify_client.py` and are **never run by the
Odoo test runner** (this package is not imported by `../__init__.py`, and the
tests are plain `pytest`, not `TransactionCase`).

## Gating

The whole module is skipped unless **both**:

- `RUN_SHOPIFY_LIVE_TESTS=1`, and
- `SHOPIFY_SHOP_DOMAIN` + `SHOPIFY_ADMIN_TOKEN` are set (env or repo-root `.env`,
  which `conftest.py` loads automatically).

## Run

```bash
# from the repo root
RUN_SHOPIFY_LIVE_TESTS=1 pytest custom_addons/odoo_ai_ops/tests/live -v
```

The bundled `pytest.ini` makes this folder pytest's rootdir and forces
`--import-mode=importlib`, so pytest imports these as top-level modules and never
imports the parent Odoo package (`custom_addons/odoo_ai_ops/__init__.py` imports
`odoo`, which isn't available under a bare `pytest`). That's also why there is no
`__init__.py` here.

In Docker (keeps secrets out of your shell history — `--env-file` injects `.env`):

```bash
docker run --rm --env-file .env -e RUN_SHOPIFY_LIVE_TESTS=1 \
  -v "$PWD:/repo" -w /repo python:3.12-slim \
  bash -c "pip install -q requests pytest && pytest custom_addons/odoo_ai_ops/tests/live -v"
```

## What runs

| Test | Effect |
|---|---|
| `test_credentials_and_connectivity` | read-only — token authenticates + resolves the store |
| `test_list_webhooks_is_readable` | read-only |
| `test_inventory_read_best_effort` | read-only (skips if no SKU) |
| `test_register_and_verify_webhooks` | **writes** — registers `orders/create` + `orders/risk_assessment_changed` HTTPS subscriptions on the store, idempotently |
| `test_webhook_secret_signs_consistently` | offline — HMAC scheme sanity on `SHOPIFY_WEBHOOK_SECRET` |
| `test_cancel_order_destructive` | **destructive**, skipped unless `SHOPIFY_LIVE_TEST_ORDER_ID` is set |
| `test_set_inventory_destructive` | **destructive**, skipped unless `SHOPIFY_LIVE_TEST_SKU` is set (restores the level afterward) |

## Webhook callback URL

Defaults to `https://barterer-dusk-retold.ngrok-free.dev/webhooks/shopify` (the
path mirrors the production edge route `POST /webhooks/{source}`). Override with
`SHOPIFY_WEBHOOK_CALLBACK_URL`, or change just the host with `SHOPIFY_NGROK_BASE`.

Registration only stores the subscription; it does not prove delivery. To watch a
real delivery end-to-end locally you need something at the callback URL that
mirrors production — verify the HMAC and forward to the agent → Odoo. In prod that
is the API Gateway → Lambda → SQS → agent path; Odoo itself never sees the raw
Shopify webhook, so don't point Shopify directly at Odoo.
