# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - 2026-07-13

### Added
- **Shopify order intake**: `orders/create` webhooks are now imported into Odoo as **confirmed `sale.order`** records (customer + line items mapped, unknown SKUs auto-created, full raw payload stored on `shopify_raw_payload`). Removes the previous assumption that a separate Shopify connector creates the order. New endpoint `POST /ai_ops/webhook/order_create` (`ai.ops.order.intake`), agent topic routing, and `sale.order` form fields/smart button.

### Changed
- **Async risk verdict**: Switched the fraud webhook from the deprecated `orders/risk` to Shopify's real `orders/risk_assessment_changed` topic. The risk assessment carries no order total, so it is correlated back to the imported `sale.order` (total recovered from it); a genuinely unknown total is escalated rather than auto-cancelled. Auto-reject and manager rejection now cancel the order in **both Shopify and Odoo**. The risk topic can fire repeatedly, so a later risky assessment can escalate a previously benign order.
- **Module dependency**: `odoo_ai_ops` now depends on `sale`.

## [Unreleased] - 2026-07-08

### Added
- **Fail-Closed Security**: Hardened default master password check in Odoo entrypoint (`templates/docker-entrypoint.sh`) to prevent insecure boots.

### Changed
- **Security Hardening**: Disabled Odoo database manager, enabled VPC-only proxy trust (`X-Forwarded-For`), enforced Slack webhook signature verification, and made Shopify cancellations refund-optional (opt-in).
- **Deployment Safety**: Configured ECS deployment circuit breakers on all services to automatically roll back failing tasks.
- **Dependency & Build Lock**: Pinned all versions (Terraform ~> 1.6, AWS providers, and Python dependencies) and locked GitHub Actions workflow setups.

## [Unreleased] - 2026-07-02

### Added
- **Testing & Alerting**: Native `terraform test` suite (plan and apply verification), Python unit tests for the agent and webhook Lambda, and CloudWatch alarms via SNS.

## [Unreleased] - 2026-07-01

### Added
- **Core AI Ops Platform**: Implemented FastAPI & LangGraph agent for automated fraud gatekeeping and missing stock reconciliation.
- **Odoo Integration**: Built the custom `odoo_ai_ops` module with task approval state machine and REST/JSON-RPC integration.
- **Webhook Pipeline**: Set up API Gateway and an HMAC-verifying Lambda proxy routing Slack/Shopify payloads to SQS.
- **Self-Hosted Telemetry**: Integrated Langfuse and ClickHouse on ECS with tiered S3 storage to avoid SaaS fees.

### Changed
- **Compute Architecture**: Migrated all services to custom Arm64 Graviton instances (Odoo, FastAPI, RDS, ClickHouse) and modularized Terraform configuration.
- **Modernized Runtime**: Upgraded the execution environment to Python 3.14.

## [Unreleased] - 2026-06-30

### Changed
- Established baseline highly-available Odoo 19 hosting stack by merging the `hosting-only` baseline and purging legacy serverless AI components.

## [Unreleased] - 2026-06-29

### Added
- Comprehensive system documentation and PlantUML C4 Context, Container, and Component diagrams.
