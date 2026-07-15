# Odoo AI Ops (`odoo_ai_ops`)

Integration layer between **Odoo 19** and the external **FastAPI + LangGraph**
agent cluster. Odoo acts as the *gatekeeper* and system-of-record; the agent
does the LLM work and human-in-the-loop orchestration over Slack.

## What it does

| Capability | Entry point |
|---|---|
| Shopify order intake (`orders/create` -> confirmed `sale.order`, raw payload stored) | `POST /ai_ops/webhook/order_create` -> `ai.ops.order.intake.process_order_create` |
| Shopify order-risk gatekeeper (cheap+risky -> auto-cancel in Shopify **and** Odoo; else escalate) | `POST /ai_ops/webhook/order_risk` -> `ai.ops.order.risk.process_webhook` |
| Start fraud workflow on the agent | `ai.ops.task.dispatch_fraud_workflow` -> `POST {AGENT}/v1/tasks/fraud` |
| Query catalog (JSON-RPC) | `ai.ops.inventory.query_catalog` |
| Historical warehouse moves (JSON-RPC) | `ai.ops.inventory.warehouse_moves` |
| Write inventory adjustment patch (JSON-RPC) | `ai.ops.inventory.apply_inventory_patch` |
| Persist manager approval/rejection | `POST /ai_ops/task/<id>/callback` -> `ai.ops.task.ai_ops_set_approval` |

## Two-event flow (order intake + async risk verdict)

Shopify's fraud analysis is **asynchronous**, so the order and its risk verdict
arrive on two separate webhooks:

* `orders/create` -> `process_order_create` builds a **confirmed `sale.order`**
  (customer + line items mapped, full payload stored on `shopify_raw_payload`).
  Unknown SKUs auto-create a minimal product so an order is never dropped.
* `orders/risk_assessment_changed` -> `process_webhook` correlates the verdict to
  that order by `shopify_order_id` and applies the cheap-order bypass below.

## The cheap-order bypass rule

`process_webhook` cancels an order **in both Shopify and Odoo with zero LLM
spend** when *both* hold:

* order total **<** `odoo_ai_ops.bypass_threshold` (default **$10**) — the risk
  webhook carries no total, so it is recovered from the correlated `sale.order`;
  when the total is genuinely unknown the order is escalated, never auto-cancelled — and
* Shopify risk level is **medium** or **high**.

Otherwise low/no-risk orders are recorded and closed, and everything else is
escalated to the LangGraph agent. The risk topic can fire repeatedly, so a later
risky assessment can still escalate a previously benign order.

## Configuration

Settings live in `ir.config_parameter` and are editable under
**Settings -> AI Ops**. In production they are seeded from the container
environment / AWS Secrets Manager (the read helper falls back to `os.environ`,
so secrets never need to be stored in the database):

| Parameter | Env fallback | Notes |
|---|---|---|
| `odoo_ai_ops.agent_base_url` | `AGENT_BASE_URL` | e.g. `http://fastapi.odoo.local:8000` |
| `odoo_ai_ops.shopify_shop_domain` | `SHOPIFY_SHOP_DOMAIN` | `my-store.myshopify.com` |
| `odoo_ai_ops.shopify_api_version` | `SHOPIFY_API_VERSION` | default `2026-07` |
| `odoo_ai_ops.shopify_webhook_callback_url` | `SHOPIFY_WEBHOOK_CALLBACK_URL` | edge ingress, e.g. `https://<cloudfront>/webhooks/shopify` |
| `odoo_ai_ops.bypass_threshold` | - | default `10.0` |
| `odoo_ai_ops.auto_reject_enabled` | - | default `True` |

Secrets are **not** UI fields — they live once in AWS Secrets Manager
(`odoo/integration/credentials`) and reach Odoo as env vars, resolved at runtime
via `os.environ` (never persisted to the database):

| Secret (env var) | Secrets Manager key |
|---|---|
| `SHOPIFY_ADMIN_TOKEN` | `shopify_admin_token` |
| `SHOPIFY_WEBHOOK_SECRET` | `shopify_webhook_secret` |
| `AI_OPS_SHARED_TOKEN` | `ai_ops_shared_token` |

### Registering the Shopify webhooks

Shopify's web UI only offers Pub/Sub or EventBridge destinations for a **custom
app**, so the plain HTTPS webhooks this integration needs must be created through
the Admin API. Rather than a one-off script, this lives as a Settings action:
set **Shopify Webhook Callback URL** (the edge ingress — CloudFront `/webhooks/shopify`,
Terraform output `webhook_url` + `/shopify`) and click **Register Shopify
Webhooks** under **Settings → AI Ops → Shopify**. It subscribes `orders/create`
and `orders/risk_assessment_changed` via `webhookSubscriptionCreate`, and is
idempotent — re-running re-points a stale URL or does nothing if already set
(`ShopifyClient.sync_webhooks`).

> The webhook HMAC Shopify signs with is the **app's API secret key**; keep
> `SHOPIFY_WEBHOOK_SECRET` (used by the ingest Lambda to verify) in sync with it,
> otherwise every delivery is rejected with a 401.

## Security

Three groups: **AI Ops User**, **AI Ops Manager**, and **AI Ops Agent
(Technical)**. The agent authenticates JSON-RPC as a dedicated user in the
*Agent* group (which implies *Stock Manager* so it can apply inventory patches).
Server-to-server controllers are guarded by a constant-time shared-token check.

## Tests

```bash
odoo -d <db> -i odoo_ai_ops --test-enable --test-tags ai_ops --stop-after-init
```
