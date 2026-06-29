# Odoo AI Ops

An event-driven, scalable, and cost-effective AI Operations pipeline integrating **Odoo 19** with external SaaS endpoints (**Shopify**, **Slack**, and **Anthropic Claude**) using **FastAPI** and **LangGraph** on AWS.

---

## 🎯 Project Overview

This project aims to automate core operations in Odoo (like inventory reconciliation and fraud detection) using AI agents while maintaining absolute security, cost-efficiency, and human oversight. 

To achieve this, the architecture is designed to:
- **Minimize SaaS overhead** by self-hosting telemetry and caching.
- **Implement Human-in-the-Loop (HITL)** mechanisms to pause agents and request validation through Slack.
- **Achieve high availability** with a localized ECS service cluster, Amazon SQS buffering, and serverless state caching.

---

## 🏗️ Architecture Highlight

The infrastructure is deployed inside a multi-AZ AWS VPC. Below is the conceptual mapping of the key components:

```
                  USER (Internet) / Webhooks
                             │
                             ▼
                [ AWS WAF + CloudFront ]
                             │
            ┌────────────────┴────────────────┐
            ▼ (Normal Traffic)                ▼ (Webhooks)
      [ AWS ALB ]                      [ AWS API Gateway ]
            │                                 │
            ▼                                 ▼
    [ Odoo 19 Service ]             [ Lambda Authorizer ]
      (ECS on EC2)                            │
            │                                 ▼
            │                           [ Amazon SQS ]
            │                                 │
            └──────────► [ Odoo SQS Workers ] ◄┘
                               │
                               ▼ (REST API)
                     [ LangGraph Agent ] 
                        (FastAPI on ECS)
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
 [ ElastiCache Valkey ]  [ Langfuse Server ]   [ NAT Gateway ]
  (Serverless State)    (ECS Fargate Spot)            │
                               │                      ▼
                               ▼             [ External APIs ]
                         [ ClickHouse ]      - Anthropic Claude
                         (Tiered to S3)      - Shopify GraphQL
                                             - Slack Block Kit
```

For more detailed diagrams, refer to the PlantUML files in the repository root:
- [C4_Context.puml](C4_Context.puml): High-level system context.
- [C4_Container.puml](C4_Container.puml): Container-level boundary diagram mapping compute, networks, and data stores.
- [C4_Component.puml](C4_Component.puml): Component relationships within the dedicated PgBouncer and FastAPI clusters.

---

## 🔄 Core Workflows

### 1. Autonomous Fraud Detection (Shopify & Slack Integration)
1. **Ingestion:** Shopify's `OrderRisk` webhook sends a payload to AWS API Gateway. A **Lambda Authorizer** validates the HMAC signature synchronously and writes it to **Amazon SQS**.
2. **Evaluation:** Odoo workers poll the SQS queue. If the flagged order is very cheap (< $10) and marked with medium or high risk, the system automatically rejects it directly in Shopify (via API) without triggering the LangGraph agent. Otherwise, it triggers a LangGraph agent run via REST API.
3. **Execution & Risk-Triage:** 
   - *Medium Risk:* Agent uses **Claude Haiku** for fast, low-cost screening.
   - *High Risk:* Agent uses **Claude Sonnet** to cross-reference IPs, shipping histories, and billing addresses.
4. **Human Gate:** The agent pauses execution, writes its active state to **Amazon ElastiCache Serverless (Valkey)**, and posts an interactive Block Kit card to Slack.
5. **Resume & Resolution:** When a store manager clicks **Approve** or **Reject** in Slack, the callback invokes the API Gateway, which wakes the agent up from Valkey to execute the final action in Shopify and Odoo.

### 2. Intelligent Inventory Reconciliation
1. An admin triggers a reconciliation action in **Odoo**.
2. The agent fetches active sales logs via the **Shopify API** and internal stock moves using local **XML-RPC** calls.
3. **Claude** processes variations, calculates correct levels, and drafts a synchronization patch.
4. The agent requests approval via Slack, updating Odoo only when validation is received.

---

## 💾 Data & Analytics Strategy

- **Agent Checkpointing:** We use **Amazon ElastiCache Serverless (Valkey)** to persist and serialize LangGraph state graphs between node cycles.
- **Zero-SaaS Telemetry:** Trace metrics and operational analytics are handled through a self-hosted pipeline:
  - **Langfuse Server:** Runs on **ECS Fargate Spot** tasks for strict cost optimization.
  - **ClickHouse Analytical Node:** Runs on an isolated **ECS EC2** instance.
  - **Tiered Storage:** Hot data resides on high-speed **Amazon io2 EBS volumes**, while cold/older trace data is dynamically offloaded to **Amazon S3** via a free Gateway VPC Endpoint, drastically lowering storage costs.

---

## 📂 Repository Layout

```
├── C4_Context.puml      # High-level architecture context
├── C4_Container.puml    # Details on subnets, DB target groups, and network interfaces
├── C4_Component.puml    # PgBouncer and FastAPI component relations
├── architecture.txt     # In-depth architectural notes & resource specifications
├── cost_analysis.txt    # Cost projections (Idle vs. Low vs. Growth tiers)
└── README.md            # Project documentation (this file)
```

---

## 💰 Cost Projections

Detailed monthly estimations (based on `us-east-1` pricing) are located in [cost_analysis.txt](cost_analysis.txt):
* **Fixed Lights-On Cost:** `~$808.00/month` (Valkey minimum floor, RDS Multi-AZ, NAT gateway, and ECS compute nodes).
* **Low Tier (~100 flagged orders/day):** `~$845.00/month`.
* **Growth Tier (~1,000 flagged orders/day):** `~$1,181.00/month`
