# Full-stack integration test

`test_fullstack.py` exercises the real webhook → agent → Odoo → Valkey → Langfuse
→ ClickHouse path against the **running `docker-compose` stack**, with the LLM
faked (no Claude call, no cost). It is the answer to "what good is a test if the
full stack isn't verified?" — it catches wiring/config bugs the mocked unit tests
can't:

* a signed Shopify `orders/create` is HMAC-verified by an edge shim (standing in
  for the production Lambda) and forwarded through the agent's real path → a real
  `sale.order` in Odoo;
* the fraud workflow runs with a **fake verdict** and pauses at the human-approval
  interrupt → its state is asserted **present in Valkey**;
* the run's trace is asserted **received by Langfuse** and **stored in ClickHouse**
  (so telemetry doesn't silently die between the two);
* `resume()` continues the paused workflow → state **advances** (not stuck).

It is **gated**: it only runs with `RUN_INTEGRATION=1`, and must run on the compose
network so it can reach the services by name. It never runs in the normal unit/CI
suite (it's skipped without the flag).

## Prerequisites

1. The stack is up and healthy: `docker compose up -d`.
2. `.env` has the Langfuse keys **without surrounding quotes** (`docker --env-file`
   and compose `env_file` pass quotes literally — quoted keys 401).
3. The agent image is built (`odoo-ai-ops-agent`).

## Run it

From the repo root:

```bash
./agent/tests/integration/run.sh
```

Or directly (Windows Git Bash needs `MSYS_NO_PATHCONV=1` so `/`-args aren't mangled):

```bash
MSYS_NO_PATHCONV=1 docker run --rm --network odoo-ai-ops_default --env-file .env \
  -e RUN_INTEGRATION=1 \
  -e LANGFUSE_HOST=http://langfuse-web:3000 -e LANGFUSE_BASE_URL=http://langfuse-web:3000 \
  -e VALKEY_URL=redis://redis:6379 \
  -e ODOO_BASE_URL=http://web:8069 -e ODOO_DB=odoo_19 -e ODOO_USERNAME=admin -e ODOO_PASSWORD=admin \
  -e ENABLE_SQS_WORKER=false -e CLICKHOUSE_HTTP=http://clickhouse:8123 \
  -v "//c/odoo/odoo-ai-ops/agent:/app" -w /app \
  odoo-ai-ops-agent \
  bash -c "python -m pip install -q pytest pytest-asyncio; python -m pytest tests/integration -v -s"
```

`ODOO_DB=odoo_19` is the local database name; adjust if yours differs
(`docker exec odoo-ai-ops-db-1 psql -U odoo -l`).

## Live-LLM tests (`test_live_llm.py`) — these cost money

`test_fullstack.py` fakes Anthropic's round-trip. `test_live_llm.py` is the
other half: **real Claude calls**, graded, with the whole telemetry chain
asserted — Valkey checkpoint → Langfuse trace → a GENERATION carrying the right
model and non-zero token usage → the row in ClickHouse.

It has its own flag on top of `RUN_INTEGRATION`, so a normal integration run
never spends anything:

```bash
RUN_LIVE_LLM=1 SHOPIFY_LIVE_TEST_SKU=<a-real-store-sku> ./agent/tests/integration/run.sh
```

| Test | What it proves |
|---|---|
| `test_high_risk_order_gets_a_real_claude_verdict` | a payload full of planted red flags is not approved, the verdict cites them, high risk routes to the strong model, and the analysis lands on the Odoo task |
| `test_clean_order_is_not_rejected` | the control — a clean order is not rejected, so "always reject" cannot pass the test above; medium risk routes to the cheap tier |
| `test_reconciliation_investigates_and_finds_the_planted_cause` | the **investigation loop** actually runs (the fake short-circuits it after one turn, so this path has never otherwise executed): the model calls read-only tools, finds an inventory adjustment planted in Odoo, and its `direction` matches the arithmetic |

The reconciliation test plants its own root cause: it baselines the Odoo count
against whatever Shopify reports for the SKU, then forces the count up by 7 with
a distinctive reason string. Odoo journals that as an `is_inventory` move
carrying the reason, which is exactly what the model's toolbelt can find.

**`SHOPIFY_LIVE_TEST_SKU` is what makes it cross-system.** With it, the Shopify
side of the comparison is a real live quantity. Without it the SKU exists only
in Odoo, Shopify reports nothing, the direction check is skipped, and the test
is no longer reconciling two systems — it still runs, but it proves less.

Reconciliation is the expensive one: a multi-turn loop on the strong model, up
to `MAX_TOOL_LOOPS` (6) round trips plus the structured verdict. Each test
prints its own token usage and cost.

## The edge shim (live Shopify webhooks, locally)

`edge_shim.py` is the production HMAC Lambda's local stand-in. There is no
Lambda/API-GW/SQS locally and we don't want any:

```
prod:   Shopify -> CloudFront -> API GW -> Lambda(HMAC) -> SQS -> agent -> Odoo
local:  Shopify -> ngrok      ->        shim(HMAC)      ------> agent -> Odoo
```

It **imports the real Lambda's `_verify_shopify`** (rather than reimplementing
it) so it can't drift from prod, hands the *same envelope* to the agent's real
`handle_sqs_message`, and Odoo still only ever sees the shared token — never an
HMAC. Every delivery (headers + raw body) is dumped to `captures/`, which is how
we learn undocumented payload shapes (Shopify publishes no sample for
`orders/risk_assessment_changed`).

```bash
./agent/tests/integration/run_edge_shim.sh          # starts on :9100
ngrok http 9100                                     # keep the same ngrok domain
./agent/tests/integration/run_edge_shim.sh logs     # watch deliveries
./agent/tests/integration/run_edge_shim.sh stop
```

Shopify keeps posting to `https://<your-ngrok-domain>/webhooks/shopify` — only the
local port ngrok targets changes (9100 instead of 80).

### Slack (the HITL half)

The shim also serves `POST /webhooks/slack` (the Lambda's other route), so
Approve/Reject clicks resume the paused workflow and write the decision to Odoo:

1. `.env` needs `SLACK_BOT_TOKEN`, `SLACK_CHANNEL` **and `SLACK_SIGNING_SECRET`**
   (Slack app → Basic Information → App Credentials → Signing Secret). Unquoted.
2. Set the Slack app's **Interactivity Request URL** to
   `https://<your-ngrok-domain>/webhooks/slack`.

Without the route you get `Status Code 404` on the buttons; without the signing
secret you get `401` (the shim logs which).

### Driving a real risk assessment

To drive a **real** risk assessment (no Shopify Payments needed), inject one with
the Admin API — the REST `orders/{id}/risks.json` endpoint is retired, the modern
equivalent is:

```graphql
mutation { orderRiskAssessmentCreate(orderRiskAssessmentInput: {
  orderId: "gid://shopify/Order/...", riskLevel: HIGH,
  facts: [{description: "...", sentiment: NEGATIVE}]
}) { orderRiskAssessment { riskLevel } userErrors { field message } } }
```

## Bugs this test surfaced (and we fixed)

- Agent `ODOO_DB=odoo` in compose, but the local DB is `odoo_19` → agent↔Odoo broken.
- `AI_OPS_SHARED_TOKEN` hard-coded in the agent's compose env, overriding `.env` →
  the agent's forwards could 401 against Odoo.
- Langfuse keys quoted in `.env` → 401 + a malformed `"http://localhost:3000"` OTLP URL.
- Valkey / ClickHouse / Langfuse had no restart policy, and ClickHouse raced the
  MinIO bucket at boot (crashed on the S3 access check) → added healthchecks,
  ordered `depends_on`, `restart:`, and `skip_access_check` on the S3 disk.
