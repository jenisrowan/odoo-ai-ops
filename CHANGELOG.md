# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased] - 2026-07-01

### Added
- **`odoo_ai_ops` Odoo 19 module** (`custom_addons/`): Shopify OrderRisk gatekeeper with cheap-order (< $10, medium/high risk) auto-rejection that bypasses the LLM, JSON-RPC reconciliation/inventory API (`query_catalog`, `warehouse_moves`, `apply_inventory_patch`), the `ai.ops.task` approval state machine, security groups, views/dashboard, settings, and tests. Verified installing + tests passing against `odoo:19.0`.
- **Path 1 (inventory reconciliation) trigger from Odoo**: `dispatch_reconciliation_workflow`, a `product.product` "Reconcile stock with AI" server action, and a form button so an admin can initiate a reconciliation AI task via REST.
- **FastAPI + LangGraph agent** (`agent/`): SQS poller, REST task API, async Odoo JSON-RPC client, Slack Block Kit + signature verification, Valkey-backed LangGraph checkpointer, Langfuse telemetry, and risk-tiered Claude (Haiku/Sonnet) fraud + reconciliation workflows with human-in-the-loop interrupts.
- **Webhook pipeline (Terraform)**: SQS queue + DLQ, API Gateway (HTTP API), the **Webhook Verify+Ingest Lambda** (`lambda/`, proxy integration: HMAC verify + Slack challenge -> SQS enqueue), the **SQS interface VPC endpoint** (PrivateLink), and CloudFront `/webhooks/*` routing so webhooks enter through the same WAF + CloudFront edge.
- **Self-hosted telemetry stack** (`modules/telemetry`): **Langfuse** (web + worker on ECS Fargate Spot), **ClickHouse** (`r6g.xlarge` ECS EC2 with an ECS-managed **io2 EBS** hot tier and **S3 cold tiering** via the S3 gateway endpoint), a dedicated **Langfuse RDS** (`db.t4g.small`), two encrypted S3 buckets, a generated `telemetry-runtime` secret, the ECS EBS infrastructure role, `Dockerfile.clickhouse` + tiered-storage config, and Langfuse/ClickHouse task definitions.
- **FastAPI ECS service**: dedicated Graviton (`m6g.large`) capacity provider, task definition, Service Connect, autoscaling, and security groups.
- **Lambda unit tests** (`lambda/tests/`) covering Shopify/Slack signature verification, the Slack challenge, replay rejection, base64 bodies, and the SQS envelope.
- **CI/CD**: `ci.yml` (ruff lint/format, agent pytest, **lambda pytest**, real Odoo 19 module tests, terraform validate) and a build+deploy workflow that builds/pushes the arm64 odoo, nginx, FastAPI and ClickHouse images, then rolls out via Terraform + `ecs update-service`.

### Changed
- **Modularized the Terraform** from a flat layout into `modules/` (network, security, data, ecr, iam, edge, webhooks, ecs, telemetry) composed by the root `main.tf`, with `moved.tf` for safe, in-place state migration.
- **Switched all compute to the exact Graviton instances from the docs and made the whole cluster arm64**: Odoo `c6g.xlarge`, PgBouncer `m6g.medium`, FastAPI `m6g.large`, ClickHouse `r6g.xlarge`, primary RDS `db.m6g.xlarge` (Multi-AZ), Langfuse RDS `db.t4g.small`; every image is built `linux/arm64` and task CPU/memory resized to fit.
- **Pinned agent dependencies for production** (no floating tags): `langgraph==1.2.6`, `langchain-anthropic==1.4.8`, `langchain-core==1.4.8`, `fastapi==0.138.0`, `anthropic==0.112.0`, `langfuse==4.12.0`, and the `python:3.12.8-slim-bookworm` base image.
- Production hardening of the Terraform: `default_tags`, `locals`/`variables`, dedicated least-privilege IAM roles, and Secrets Manager bindings for the Odoo and FastAPI tasks.
- `docker-compose.yml` now runs Nginx as a true sidecar (shared network namespace) mirroring the ECS task, mounts the custom addon, and adds the agent for a full local stack.
- `odoo-task.json` injects the AI Ops integration env/secrets; ECS routes ALB traffic to the Nginx container.
- **Docs updated to reflect the implementation**: the C4 diagrams, `architecture.txt`, `README.md`, and `cost_analysis.txt` now describe the webhook flow as a single proxy Lambda (verify + Slack challenge + SQS enqueue) and include the telemetry stack and exact instances.

### Removed
- Dead `opensearch` provider and `aws_opensearchserverless_collection.bedrock_kb` reference in `provider.tf` (no Bedrock/OpenSearch in the documented architecture - Anthropic API is reached via NAT), plus the unused `bedrock-runtime` VPC endpoint. These would have failed `terraform validate`.

## [Unreleased] - 2026-06-30

### Changed
- Merged the `hosting-only` branch from `odoo-aws-cloud` (`feature/hosting-only`) to establish a clean, stable baseline for highly-available Odoo 19 hosting by removing legacy AWS serverless AI components (Amazon Bedrock, OpenSearch Serverless, Lambda integrations) and custom addons.

## [Unreleased] - 2026-06-29

### Added
- Comprehensive architecture documentation for the Odoo AI Ops infrastructure (`architecture.txt`).
- High-level C4 Context diagram mapping interactions with external services and users (`C4_Context.puml`).
- C4 Container diagram outlining internal services and data flow (`C4_Container.puml`).
- C4 Component diagram detailing component-level architecture (`C4_Component.puml`).
