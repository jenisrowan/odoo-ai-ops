# Odoo AI Ops on AWS

An event-driven, scalable, and cost-effective AI Operations pipeline integrating **Odoo 19** with external SaaS endpoints (**Shopify**, **Slack**, and **Anthropic Claude**). Powered by **FastAPI** and **LangGraph**, and deployed using a production-ready **Terraform** infrastructure on AWS.

---

## 🎯 Project Overview

This project aims to automate core operations in Odoo (like inventory reconciliation and fraud detection) using AI agents while maintaining absolute security, cost-efficiency, and human oversight. 

To achieve this, the architecture is designed to:
- **Minimize SaaS overhead** by self-hosting telemetry and caching.
- **Implement Human-in-the-Loop (HITL)** mechanisms to pause agents and request validation through Slack.
- **Achieve high availability** with a localized ECS service cluster, Amazon SQS buffering, and serverless state caching.
- **Provide a robust, automated infrastructure** managed entirely via Terraform, utilizing AWS best practices for load balancing, database connection pooling, and zero-knowledge secrets.

---

## 🏗️ Architecture & Infrastructure

The infrastructure is deployed inside a multi-AZ AWS VPC.
```

                  USER (Internet) / Webhooks
                             │
                             ▼
                 [ AWS WAF + CloudFront ]
                             │
             ┌───────────────┴──────────────────┐
             ▼ (Normal Traffic)                 ▼ (Webhooks)
       [ AWS ALB ]                        [ AWS API Gateway ]
             │                                  │
             ▼                                  ▼
      [ Nginx Sidecar ]                [ Verify+Ingest Lambda ]
             │                                  │
             ▼                                  ▼
     [ Odoo 19 Service ]                  [ Amazon SQS ]
       (ECS on EC2)                             │
             │                                  │
             └──────────► [ Odoo SQS Workers ] ◄┘
                               │
                               ▼ (REST API)
                     [ LangGraph Agent ] 
                      (FastAPI on ECS)
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
 [ ElastiCache Valkey ]  [ Langfuse Server ]   [ NAT Gateway ]
  (Serverless State)    (ECS Fargate Spot)           │
                               │                     ▼
                 ┌─────────────┴─────────────┐   [ External APIs ]
                 ▼                           ▼     - Anthropic Claude
           [ ClickHouse ]           [ Langfuse RDS ]- Shopify GraphQL
           (Tiered to S3)             (Postgres)    - Slack Block Kit

```
### Core Infrastructure Components

* **Compute:** Odoo, Nginx, and FastAPI run on ECS EC2 capacity providers. PgBouncer runs as a high-availability layer to manage database connection pooling efficiently, communicating via ECS Service Connect.
* **Database & Storage:** Amazon RDS for PostgreSQL (Multi-AZ) handles application data. Amazon EFS provides a shared file system for Odoo's `filestore`, utilizing a triple-tier lifecycle policy (Primary -> Infrequent Access -> Archive) to optimize long-term storage costs.
* **Cache & State:** **Amazon ElastiCache for Valkey (Serverless)** serves a dual purpose: it acts as a centralized session store for Odoo (via `mangono-odoo-redis-session`) and persists LangGraph state graphs between node cycles.
* **Global Delivery & Security:** AWS CloudFront provides edge caching, protected by a global AWS WAF with IP reputation and bot control rules.

---

## 🔄 Core AI Workflows

### 1. Autonomous Fraud Detection (Shopify & Slack Integration)

1. **Ingestion:** Shopify's `OrderRisk` webhook sends a payload to AWS API Gateway, which invokes a **Lambda proxy integration** that validates the HMAC signature (and answers Slack's challenge) synchronously and, on success, writes the verified payload to **Amazon SQS**.
2. **Evaluation:** Odoo workers poll the SQS queue. If the flagged order is very cheap (< $10) and marked with medium or high risk, the system automatically rejects it directly in Shopify (via API). Otherwise, it triggers a LangGraph agent run via REST API.
3. **Execution & Risk-Triage:**
* *Medium Risk:* Agent uses **Claude Haiku** for fast, low-cost screening.
* *High Risk:* Agent uses **Claude Sonnet** to cross-reference IPs, shipping histories, and billing addresses.


4. **Human Gate:** The agent pauses execution, writes its active state to **Valkey**, and posts an interactive Block Kit card to Slack.
5. **Resume & Resolution:** When a store manager clicks **Approve** or **Reject** in Slack, the callback invokes the API Gateway, which wakes the agent up from Valkey to execute the final action.

### 2. Intelligent Missing Stock Resolution

1. An admin flags a stock mismatch for a specific product in **Odoo** and **Shopify**.
2. The agent fetches recent sales and fulfillment logs via the **Shopify API** and traces internal warehouse moves using local **XML-RPC** calls.
3. **Claude** analyzes the data to pinpoint the discrepancy, calculates the accurate physical inventory level, and drafts an inventory adjustment patch.
4. The agent summarizes its findings and requests approval via **Slack**, updating Odoo's inventory only when human validation is received.

---

## 💾 Telemetry & Cost Optimization

### Zero-SaaS Telemetry

Trace metrics and operational analytics are handled through a self-hosted pipeline to avoid expensive third-party SaaS fees:

* **Langfuse Server:** Runs on **ECS Fargate Spot** tasks for strict cost optimization.
* **ClickHouse Analytical Node:** Tiered storage offloads older trace data to Amazon S3 via a free Gateway VPC Endpoint.

### Networking Cost Savings

To reduce configuration complexity and minimize costs, this project utilizes a **Regional NAT Gateway** pattern. Sharing a single NAT Gateway across private subnets avoids the fixed hourly charges of multiple AZ-specific gateways while maintaining outbound internet access.

---

## 🔒 Security

* **Zero-Knowledge Secrets Management:**
* **RDS Managed Passwords:** The master database password is automatically generated, encrypted, and managed natively by AWS. Plain-text credentials are **never** exposed or stored in the Terraform `tfstate` file.
* **Direct Secret Injection:** ECS tasks use IAM Task Execution Roles to fetch credentials directly from AWS Secrets Manager at runtime.


* **Network Isolation:** RDS and ElastiCache reside in private subnets, accepting traffic only from authorized ECS tasks.

---

## 🚀 Getting Started

### Prerequisites

* [Terraform](https://www.terraform.io/downloads.html) (>= 1.1)
* AWS CLI configured with appropriate permissions.
* Docker (for building and pushing custom images to ECR).

### Deployment

1. **Setup AWS Secrets**: Before deploying, manually create two secrets in AWS Secrets Manager:

   a. **Odoo master password** - `odoo/admin/password`, *Other type of secret*, key `password` = your master password.

   b. **Integration credentials** - `odoo/integration/credentials`, *Other type of secret*, a JSON
   document with these keys (consumed by the Odoo, FastAPI, and Lambda tasks):

   ```json
   {
     "ai_ops_shared_token": "...",
     "shopify_admin_token": "...",
     "shopify_shop_domain": "my-store.myshopify.com",
     "shopify_webhook_secret": "...",
     "slack_bot_token": "...",
     "slack_signing_secret": "...",
     "anthropic_api_key": "...",
     "odoo_agent_password": "...",
     "langfuse_public_key": "...",
     "langfuse_secret_key": "..."
   }
   ```

2. **Initialize and Apply Terraform** (or run the `Build & Deploy` GitHub Action, which builds and
   pushes all three images then applies):
   ```bash
   cd terraform
   terraform init
   terraform apply
   ```

3. **Create the agent's Odoo user**: after first boot, create an Odoo user with login
   `ai_ops_agent` (see `odoo_agent_username` var), set its password to `odoo_agent_password`, and
   grant it the **AI Ops Agent (Technical)** group. The FastAPI agent authenticates as this user
   over JSON-RPC.

4. **Point the webhooks at API Gateway**: configure Shopify (`orders/risk`) and Slack
   (interactivity + events) to POST to the `api_gateway_webhook_url` output
   (`/webhooks/shopify` and `/webhooks/slack`).



### Outputs

After deployment, Terraform will output:

* `cloudfront_url`: The primary public URL for your Odoo instance.
* `alb_url`: Internal load balancer URL.
* `odoo_ecr_url` / `nginx_ecr_url` / `fastapi_ecr_url`: Target ECR repositories for your images.
* `api_gateway_webhook_url`: Public webhook ingress (append `/webhooks/shopify` or `/webhooks/slack`).
* `webhook_queue_url`: SQS queue the agent polls privately via PrivateLink.

---

## 📂 Repository Layout

```
├── .github/workflows/   # CI/CD pipelines (ci.yml, aws.yml build+deploy, destroy.yml)
├── agent/               # FastAPI + LangGraph agent service (app/, tests/)
├── custom_addons/
│   └── odoo_ai_ops/     # Custom Odoo 19 module (gatekeeper, JSON-RPC API, approvals)
├── lambda/
│   └── webhook_authorizer/  # API Gateway Lambda (HMAC verify + Slack challenge + SQS)
├── docs/                # Architecture diagrams (PlantUML) and deep-dive notes
├── templates/           # Dockerfiles (odoo, nginx, fastapi), entrypoints, ECS task defs
├── terraform/           # Infrastructure as Code (ECS, RDS, Valkey, SQS, API GW, Lambda)
├── CHANGELOG.md         # Version history and release notes
├── cost_analysis.txt    # AWS monthly cost projections and tiers
├── docker-compose.yml   # Local development environment (nginx sidecar + agent)
└── README.md            # Project documentation (this file)
```
