# AI Ops Agent (FastAPI + LangGraph)

The agent cluster from the architecture's *Agent Backend*. It polls SQS, forwards
Shopify order-risk webhooks to the Odoo gatekeeper, runs the LangGraph fraud /
reconciliation workflows (Claude Haiku for medium risk, Sonnet for high),
checkpoints human-in-the-loop state to **Valkey**, posts Slack Block Kit cards,
and flushes telemetry to **Langfuse**.

## Layout

```
agent/
├── app/
│   ├── main.py            # FastAPI app + lifespan (boots runtime + SQS worker)
│   ├── runtime.py         # AgentRuntime: clients, graphs, event routing
│   ├── config.py          # env-driven Settings
│   ├── odoo_client.py     # async JSON-RPC + webhook forwarding
│   ├── slack_client.py    # Block Kit + signature verification
│   ├── checkpointer.py    # Valkey-backed LangGraph checkpointer
│   ├── telemetry.py       # Langfuse callback handler
│   ├── llm.py             # risk-tiered ChatAnthropic factory
│   ├── sqs_worker.py      # long-polling SQS consumer
│   ├── security.py        # bearer-token dependency
│   ├── routers/           # /healthz, /v1/tasks/*
│   └── graph/             # fraud_graph, reconciliation_graph, state
└── tests/
```

## Endpoints

| Method | Path | Caller | Purpose |
|---|---|---|---|
| POST | `/v1/tasks/fraud` | Odoo | start fraud workflow (202 + `run_id`) |
| POST | `/v1/tasks/reconciliation` | Odoo | start reconciliation workflow |
| GET  | `/healthz` | ALB/ECS | liveness |

The Shopify webhook **ingress** is *not* an HTTP endpoint here - it arrives via
SQS (API Gateway -> Lambda -> SQS) and is consumed by the SQS worker, which
routes by topic: `orders/create` -> Odoo `POST /ai_ops/webhook/order_create`
(imports the `sale.order`); `orders/risk_assessment_changed` (or the legacy
`orders/risk`) -> `POST /ai_ops/webhook/order_risk` (the fraud gatekeeper).

## Human-in-the-loop

`fraud_graph` pauses at `await_decision` (`interrupt()`); the state graph is
serialized to Valkey and the thread terminates. A manager's Slack click is
delivered (API Gateway -> Lambda -> SQS) and the SQS worker calls
`runtime.resume(run_id, decision)`, which rehydrates the graph from Valkey and
runs `finalize`, writing the decision back to Odoo over JSON-RPC.

## Local dev

```bash
cd agent
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in values
uvicorn app.main:app --reload
pytest
ruff check .
```
