# Odoo AI Ops (`odoo_ai_ops`)

Integration layer between **Odoo 19** and the external **FastAPI + LangGraph**
agent cluster. Odoo acts as the *gatekeeper* and system-of-record; the agent
does the LLM work and human-in-the-loop orchestration over Slack.

## What it does

| Capability | Entry point |
|---|---|
| Shopify order-risk gatekeeper (cheap+risky -> auto-cancel; else escalate) | `POST /ai_ops/webhook/order_risk` -> `ai.ops.order.risk.process_webhook` |
| Start fraud workflow on the agent | `ai.ops.task.dispatch_fraud_workflow` -> `POST {AGENT}/v1/tasks/fraud` |
| Query catalog (JSON-RPC) | `ai.ops.inventory.query_catalog` |
| Historical warehouse moves (JSON-RPC) | `ai.ops.inventory.warehouse_moves` |
| Write inventory adjustment patch (JSON-RPC) | `ai.ops.inventory.apply_inventory_patch` |
| Persist manager approval/rejection | `POST /ai_ops/task/<id>/callback` -> `ai.ops.task.ai_ops_set_approval` |

## The cheap-order bypass rule

`process_webhook` cancels an order **directly in Shopify with zero LLM spend**
when *both* hold:

* order total **<** `odoo_ai_ops.bypass_threshold` (default **$10**), and
* Shopify risk level is **medium** or **high**.

Otherwise low/no-risk orders are recorded and closed, and everything else is
escalated to the LangGraph agent.

## Configuration

Settings live in `ir.config_parameter` and are editable under
**Settings -> AI Ops**. In production they are seeded from the container
environment / AWS Secrets Manager (the read helper falls back to `os.environ`,
so secrets never need to be stored in the database):

| Parameter | Env fallback | Notes |
|---|---|---|
| `odoo_ai_ops.agent_base_url` | `AGENT_BASE_URL` | e.g. `http://fastapi.odoo.local:8000` |
| `odoo_ai_ops.shared_token` | `AI_OPS_SHARED_TOKEN` | bearer shared with the agent |
| `odoo_ai_ops.shopify_shop_domain` | `SHOPIFY_SHOP_DOMAIN` | `my-store.myshopify.com` |
| `odoo_ai_ops.shopify_admin_token` | `SHOPIFY_ADMIN_TOKEN` | Shopify Admin API token |
| `odoo_ai_ops.shopify_api_version` | `SHOPIFY_API_VERSION` | default `2025-01` |
| `odoo_ai_ops.bypass_threshold` | - | default `10.0` |
| `odoo_ai_ops.auto_reject_enabled` | - | default `True` |

## Security

Three groups: **AI Ops User**, **AI Ops Manager**, and **AI Ops Agent
(Technical)**. The agent authenticates JSON-RPC as a dedicated user in the
*Agent* group (which implies *Stock Manager* so it can apply inventory patches).
Server-to-server controllers are guarded by a constant-time shared-token check.

## Tests

```bash
odoo -d <db> -i odoo_ai_ops --test-enable --test-tags ai_ops --stop-after-init
```
