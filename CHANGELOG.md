# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - 2026-06-28

### Added
- Introduced **AWS API Gateway** and **AWS Lambda Authorizer** to handle secure, HMAC-validated webhook ingestion from external services.
- Introduced **Amazon SQS** queue to act as a durable, asynchronous buffer between webhook ingestion and Odoo processing.
- Created `cost_analysis.txt` detailing baseline fixed costs, per-transaction variable cost models, scaling projections (Idle, Low, Growth), cost optimization levers, and performance/budget risk flags.

### Changed
- Updated `architecture.txt` to reflect the decoupled AWS ElastiCache Serverless (Valkey) checkpointer, ClickHouse tiered storage (EBS gp3 and S3 Gateway Endpoint), signature verification using AWS Lambda Authorizer + SQS, and dynamic LLM routing (Claude Haiku for medium-risk, Claude Sonnet for high-risk, and bypass for low-risk).
- Refactored `C4_Context.puml` to include administrative ERP management connections and standardize Anthropic Claude engine representations.
- Redesigned `C4_Container.puml` to visualize the updated networking topology (routing SQS and external APIs through the NAT Gateway, routing S3 through Gateway VPC Endpoints), add the Lambda Authorizer, integrate ElastiCache Serverless (Valkey), and cleanup layout routing using `LAYOUT_TOP_DOWN()`.
- Enhanced `C4_Component.puml` to map the Odoo NFS mount to Amazon EFS, represent bidirectional telemetry/state persistence flow with Valkey, and correct webhook validation flows to API Gateway.

## [Unreleased] - 2026-06-27
### Added
- Comprehensive architecture documentation for the Odoo AI Ops infrastructure (`architecture.txt`).
- High-level C4 Context diagram mapping interactions with external services and users (`C4_Context.puml`).
- C4 Container diagram outlining internal services and data flow (`C4_Container.puml`).
- C4 Component diagram detailing component-level architecture (`C4_Component.puml`).
