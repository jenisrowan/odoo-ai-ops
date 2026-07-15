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
