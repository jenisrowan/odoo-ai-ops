# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
