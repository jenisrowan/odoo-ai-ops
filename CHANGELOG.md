# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The platform is pre-launch, so nothing has been released yet — every change lives
under this single `Unreleased` heading, newest first within each category. The
per-day development history is preserved in the git log. On the first release this
block becomes `## [1.0.0] - <date>` and a fresh empty `Unreleased` opens above it.

### Added
- **Reconciliation is now an investigation, not a single guess**: the graph is
  `gather → investigate ⇄ tools → propose`, where Claude works the case with a read-only Odoo
  toolbelt (`get_discrepancy_context`, `list_stock_moves`, `get_move_details`,
  `list_sale_order_lines`, `search_products`) instead of being handed one fixed evidence blob.
  Diagnosing a stock divergence is genuinely non-linear — the second question depends on the
  first answer — unlike fraud triage, which stays a single judgement over a payload already in
  hand. `gather` still makes one deterministic `discrepancy_context` call so the Slack card
  always carries real numbers, and the loop is capped at `MAX_TOOL_LOOPS` (tools are simply not
  offered on the final turn, which also prevents a dangling `tool_use` block).
  New supporting Odoo readers: `ai.ops.inventory.move_details` (drills into a suspect move
  *including its picking*, which is what distinguishes "shipped but never validated in Odoo" from
  "still in the warehouse") and `sale_order_lines`.
- **Root-cause coverage for the causes that actually occur.** The snapshot previously searched
  only `location_dest_id.usage = customer` and `location_id.usage = supplier`, so three common
  causes were invisible to the investigation:
  - *Someone forced the on-hand count* — `recent_inventory_adjustments` (moves with
    `is_inventory`), carrying **who did it** (`user`) and the reason they gave
    (`adjustment_reason`). Nothing else in the system revealed this.
  - *Stock moved to another warehouse* — `stock_by_location` / the `get_stock_by_location` tool:
    quantity per location and warehouse, reserved vs available, last count date. Odoo's headline
    on-hand sums every internal location, so this leaves the total unchanged and shows up nowhere
    else; it's the answer when the totals disagree but the move history is clean.
  - *A stuck internal transfer* — `pending_internal_moves`, previously misfiled as nothing at all
    (an internal move is neither customer-bound nor supplier-sourced).

  All moves are now tagged with a `kind` (`incoming`, `outgoing`, `internal_transfer`,
  `inventory_adjustment`, `scrap`, `other`) and their author, via one shared serializer, and
  `warehouse_moves` accepts a `kinds` filter. The analytical framework in the prompt names each
  cause and which evidence bucket rules it in or out.
- **`ledger_check` / `check_stock_ledger`** — a consistency canary, not a root cause. Verified in
  an Odoo shell that everything an operator can do is journalled: `stock.move` /
  `stock.move.line` is the ledger, `stock.quant` is the running balance, on-hand edits go through
  the *counted* quantity and are recorded as `is_inventory` moves, and `stock.quant.quantity` is
  a `readonly` field. A sweep of every `stock.*` and `mail.*` model confirmed the invariant can
  only be broken by code writing that readonly field through the ORM — a bug in whatever did it,
  not an operator action. The check nets done moves against quants and, on a gap, tells the model
  to report the inconsistency itself and escalate rather than explain a Shopify difference with
  it. Both sides are computed over the same location set: pairing the move ledger with
  `qty_available` (which only counts internal locations *under a warehouse view location*) would
  have reported a phantom gap for stock in an unattached internal location — regression test
  `test_ledger_does_not_cry_wolf_outside_a_warehouse_tree` pins this.
- **Shopify orders-by-SKU lookup** (`ShopifyClient.list_orders_for_sku`, exposed as the
  `list_shopify_orders` tool via `ai.ops.inventory.shopify_orders_for_sku`): recent Shopify orders
  containing a SKU, with each order's financial and fulfillment status. This closes an evidence
  gap — `create_missing_sale_order` was already in the verdict enum but nothing could produce the
  evidence to justify choosing it. Line items are filtered to the requested SKU client-side
  (Shopify matches an order on *any* of its SKUs, so reporting the rest would invent a missing
  sale), and a Shopify outage returns an error row rather than raising, so the investigation
  continues on Odoo-side evidence. Verified against Admin API 2026-07: `sku:` is a supported
  `orders` filter and every field used is current and undeprecated.
- **The model never gets a write tool.** The toolbelt is read-only and `apply` stays plain Python
  dispatching on the verdict's action enum, so the model's output is a proposal in a closed
  vocabulary rather than a call. The evidence it reads is attacker-influenced (product names,
  order notes, Shopify fields); a write tool would put "the model was talked into it" one prompt
  injection away from moving stock, with only the Slack card in the way.
- **Automated production Odoo bootstrap**: the Odoo entrypoint now runs an idempotent
  `templates/odoo-bootstrap.py` before starting the server - creates the production database if
  missing, installs `odoo_ai_ops`, and provisions the agent's technical user from the
  `odoo_agent_password` secret (password re-applied each boot, so secret rotation propagates on
  deploy). Removes the manual "create the agent's Odoo user" deploy step. Verified in Docker:
  first boot (create+install+user), idempotent re-boot, and password rotation.
- **Slack decision confirmations**: after a manager decides, the fraud approval card is updated
  in place (`chat.update`) - buttons are replaced with "Approved/Rejected by <manager>" - and
  reconciliation outcomes are confirmed as a threaded reply on the diagnosis message.
- **Shopify order intake**: `orders/create` webhooks are now imported into Odoo as **confirmed `sale.order`** records (customer + line items mapped, unknown SKUs auto-created, full raw payload stored on `shopify_raw_payload`). Removes the previous assumption that a separate Shopify connector creates the order. New endpoint `POST /ai_ops/webhook/order_create` (`ai.ops.order.intake`), agent topic routing, and `sale.order` form fields/smart button.
- **Fail-Closed Security**: Hardened default master password check in Odoo entrypoint (`templates/docker-entrypoint.sh`) to prevent insecure boots.
- **Testing & Alerting**: Native `terraform test` suite (plan and apply verification), Python unit tests for the agent and webhook Lambda, and CloudWatch alarms via SNS.
- **Core AI Ops Platform**: Implemented FastAPI & LangGraph agent for automated fraud gatekeeping and missing stock reconciliation.
- **Odoo Integration**: Built the custom `odoo_ai_ops` module with task approval state machine and REST/JSON-RPC integration.
- **Webhook Pipeline**: Set up API Gateway and an HMAC-verifying Lambda proxy routing Slack/Shopify payloads to SQS.
- **Self-Hosted Telemetry**: Integrated Langfuse and ClickHouse on ECS with tiered S3 storage to avoid SaaS fees.
- Comprehensive system documentation and PlantUML C4 Context, Container, and Component diagrams.

### Changed
- **Payload parsing matches reality**: `_extract` in the order-risk gatekeeper now parses exactly
  the real `orders/risk_assessment_changed` shape (flat `order_id` + `risk_level`, verified
  against captured production deliveries). Removed the speculative REST-era layer — nested
  order/assessment objects, assessment lists, camelCase keys, `recommendation` aliases, and GID
  fallbacks that would have silently broken order correlation had they ever fired. Order intake
  identity is strictly `order["id"]`, and the address country resolves from `country_code` only
  (the full-name `country` key can never match an ISO-code search). Unit and integration tests
  now feed the real captured payload shapes instead of an invented envelope.
- **Async risk verdict**: Switched the fraud webhook from the deprecated `orders/risk` to Shopify's real `orders/risk_assessment_changed` topic. The risk assessment carries no order total, so it is correlated back to the imported `sale.order` (total recovered from it); a genuinely unknown total is escalated rather than auto-cancelled. Auto-reject and manager rejection now cancel the order in **both Shopify and Odoo**. The risk topic can fire repeatedly, so a later risky assessment can escalate a previously benign order.
- **Module dependency**: `odoo_ai_ops` now depends on `sale`.
- **Security Hardening**: Disabled Odoo database manager, enabled VPC-only proxy trust (`X-Forwarded-For`), enforced Slack webhook signature verification, and made Shopify cancellations refund-optional (opt-in).
- **Deployment Safety**: Configured ECS deployment circuit breakers on all services to automatically roll back failing tasks.
- **Dependency & Build Lock**: Pinned all versions (Terraform ~> 1.6, AWS providers, and Python dependencies) and locked GitHub Actions workflow setups.
- **Compute Architecture**: Migrated all services to custom Arm64 Graviton instances (Odoo, FastAPI, RDS, ClickHouse) and modularized Terraform configuration.
- **Modernized Runtime**: Upgraded the execution environment to Python 3.14.
- Established baseline highly-available Odoo 19 hosting stack by merging the `hosting-only` baseline and purging legacy serverless AI components.

### Fixed
- **Reconciliation compared Odoo's total on-hand against Shopify, which is wrong for any store
  that doesn't sell its entire stock online.** In the ordinary shop-location + back-warehouse
  setup Odoo sums both while Shopify only sees the shop, so Odoo looked permanently higher — and
  the analysis read that standing gap as a Shopify undercount and would keep "correcting" Shopify
  upward, overselling stock sitting in the back. New **Shopify Stock Location** setting
  (`odoo_ai_ops.shopify_stock_location_id`, AI Ops settings) scopes the Odoo side to the location
  subtree that actually backs the channel. When it is unset *and* the product is stocked in more
  than one internal location, `discrepancy_context` returns an explicit `location_scope.warning`
  and the prompt instructs the model to recommend `no_action` and ask a human to configure it
  rather than touch Shopify. A single-location store needs no configuration and gets no warning.
- **Fraud analysis now sees the order**: the agent used to receive the bare risk webhook, which
  identifies the order but contains nothing to analyse — the Slack card rendered "Order total: ?"
  and the LLM analysed an empty context. `dispatch_fraud_workflow` now sends the correlated
  order's name/total/currency plus the fraud-relevant subset of the preserved `orders/create`
  payload (addresses, customer, contact, client details/IP, payment gateways, line items).
- **`pending` risk no longer auto-cancels**: Shopify's `pending` assessment (analysis still
  running) was mapped to medium risk, so a cheap order could be cancelled before the verdict
  existed. It now maps to `none` — recorded only; the dedup guard already lets the later real
  verdict escalate.
- **Custom addons now ship in the Odoo image**: `Dockerfile.odoo` bakes `custom_addons/` into
  `/mnt/extra-addons` (the path `odoo.conf`'s `addons_path` already expected); previously the
  module never reached the production image at all.
- **Double-resume guard**: `AgentRuntime.resume()` now refuses to resume unknown or
  already-completed workflows (SQS redelivers at-least-once and Slack cards could be clicked
  twice, which re-executed the final nodes and double-wrote the decision to Odoo). A stale click
  gets a threaded "already decided" notice instead.

### Security
- **The agent's Odoo credential is now constrained, not just its code path.** The read-only
  toolbelt and `_require_approved_task` both live above Odoo, so neither limited what someone
  holding the agent's JSON-RPC password could do with a direct `execute_kw` — and the agent user
  implied `stock.group_stock_manager`, which carries unlink on `stock.move` and full write/unlink
  on warehouses, locations, routes and putaway rules. It now implies `stock.group_stock_user`
  (required: `stock.quant._is_inventory_mode()` gates the adjustment flow on that group via
  `env.user`, which `sudo()` does not satisfy), and global record rules deny it write/create/unlink
  on `stock.quant`, `stock.move`, `stock.move.line`, `stock.picking` and `stock.lot`.
  `ai.ops.inventory`'s write methods elevate internally *after* the approval gate, so the gated
  path still works and is now the only route from that credential to stock. Reads are elevated
  too, with explicit company scoping (sudo bypasses the multi-company rule) and a pinned field
  list, so "read-only" does not quietly mean "read every field". Covered by
  `TestAgentCredentialIsConstrained`, which pins both directions: the direct writes fail, the
  approved adjustment still lands, and other users are unaffected by the global rules.
