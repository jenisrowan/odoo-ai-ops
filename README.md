# Odoo on AWS (ECS & Terraform)

This project provides a production-ready infrastructure for deploying **Odoo 19** on AWS using Terraform. It leverages AWS ECS (Elastic Container Service) with EC2 capacity providers, Amazon RDS for PostgreSQL, and Amazon EFS for persistent storage.

## Architecture

- **Global Delivery**: AWS CloudFront provides HTTPS termination and edge caching for static assets.
- **Load Balancing**: An Application Load Balancer (ALB) distributes traffic to Odoo tasks.
- **Compute (Odoo)**: Odoo and Nginx run as sidecar containers on dedicated `m7i-flex.large` instances.
- **PgBouncer Pooling**: A High-Availability PgBouncer layer (2 tasks) runs on dedicated `t3.micro` instances to manage database connection pooling efficiently.
- **Service Discovery**: ECS Service Connect (`odoo.local`) provides seamless internal communication between Odoo and PgBouncer using `pgbouncer.odoo.local`.
- **Database**: Amazon RDS for PostgreSQL (Multi-AZ) handles application data.
- **Cache**: **Amazon ElastiCache for Valkey** (Serverless) provides high-performance, asynchronous status tracking and coordination for long-running AI research tasks.
- **AI Research**: **Amazon Bedrock** integration with a Supervisor Agent, Knowledge Base (using **Amazon OpenSearch Serverless** as a vector store), and S3-based document vault.
- **Storage**: Amazon EFS provides a shared file system for Odoo's `filestore` and sessions.
- **Networking**: A VPC with both public and private subnets, secured with specialized Security Groups for ALB, Tasks, PgBouncer, and RDS.

## Prerequisites

- [Terraform](https://www.terraform.io/downloads.html) (>= 1.1)
- [AWS CLI](https://aws.amazon.com/cli/) configured with appropriate permissions.
- Docker (for building and pushing custom images to ECR).

## Getting Started

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/jenisrowan/odoo-aws-cloud.git
    cd odoo-aws-cloud
    ```

2.  **Initialize Terraform**:
    ```bash
    cd terraform
    terraform init
    ```

3.  **Setup AWS Secrets**:
    Before deploying, you must manually create two secrets in AWS Secrets Manager:
    - **Odoo Application Password**:
        - Choose **Other type of secret**.
        - Add a Key/Value pair: Key = `password`, Value = `[Your_Secure_Master_Password]`.
        - Name the secret: `odoo/admin/password`.
    - **Tavily API Key (for AI Research)**:
        - Choose **Other type of secret**.
        - Add a Key/Value pair: Key = `api_key`, Value = `[Your_Tavily_API_Key]`.
        - Name the secret: `tavily/api_key`.

4.  **Deploy**:
    ```bash
    cd terraform
    terraform apply
    ```

## AI-Powered Customer Research

This infrastructure includes a built-in AI research pipeline using Amazon Bedrock:
- **Supervisor Agent**: Orchestrates research tasks using Claude 4.6 Sonnet.
- **Web Search**: A "Librarian" Lambda function searches the web via **Tavily** for real-time news.
- **Knowledge Base**: An RAG (Retrieval-Augmented Generation) system backed by **OpenSearch Serverless** and **Amazon Nova-2** allows the agent to search internal PDFs and 10-K filings stored in S3.
- **Odoo Integration**: The Odoo backend synchronously invokes Bedrock and securely streams the generated reports directly into CRM records.

## Performance & Tuning

## Cost Optimization & Networking

### Regional NAT Gateway
To reduce configuration complexity and minimize costs, this project utilizes a **Regional NAT Gateway** pattern:
- **Cost Savings**: By using a single Regional NAT Gateway shared across private subnets, we avoid the fixed hourly charges of multiple NAT Gateways in different Availability Zones.
- **Data Transfer**: This design is especially cost-effective if certain AZs have no active resources, as it eliminates NAT-related idle costs while maintaining outbound internet access for private workloads.
- **Smarter Traffic**: Zero multi-AZ transfer charges for regional NAT gateways, as AWS will pick the NAT in the current AZ for outbound traffic.

### Valkey for Caching
We use **Valkey 8.2 (Serverless)** through Amazon ElastiCache. Valkey is a fully open-source, high-performance alternative to Redis that offers significant cost savings (up to 33% with Serverless) while remaining fully compatible with Redis clients.
- **Auto-Scaling**: The serverless configuration scales dynamically based on workload.
- **State Management**: Used for real-time status tracking of long-running AI research tasks and agent coordination.

### EFS Storage Tiering
To further optimize long-term storage costs for Odoo's filestore, a triple-tier lifecycle policy is implemented:
- **Infrequent Access (IA)**: Files not accessed for **30 days** automatically move to the IA tier (significantly cheaper storage).
- **Archive Tier**: Files not accessed for **90 days** move to EFS Archive storage for maximum cost savings.
- **Intelligent Recovery**: Any access to a file in IA or Archive immediately transitions it back to **Primary Storage** (AFTER_1_ACCESS) to ensure low-latency performance and cut down on access cost for active data.

## Security

- **Web Application Firewall (WAF)**: A global AWS WAF protects the CloudFront distribution with rules for IP reputation, bot control, and rate limiting against malicious traffic.
- **Database**: RDS is situated in private subnets and **only accepts traffic from PgBouncer tasks** on port 5432.
- **PgBouncer**: Only accepts traffic from Odoo tasks on port 6432.
- **Zero-Knowledge Secrets Management**: 
  - **RDS Managed Passwords**: The master database password is automatically generated, encrypted, and managed natively by AWS. Plain-text credentials are **never** exposed or stored in the Terraform `tfstate` file. 
  - **Direct Secret Injection**: ECS tasks use IAM Task Execution Roles to fetch credentials directly from AWS Secrets Manager at runtime. Passwords are never passed as plain-text environment variables, protecting them from exposure in the AWS Console or logs. 
- **Encryption**: EFS is encrypted at rest, and CloudFront enforces HTTPS.

## Outputs

After deployment, Terraform will output:
- `cloudfront_url`: The primary public URL for your Odoo instance.
- `alb_url`: Internal load balancer URL (for testing).
- `odoo_ecr_url` / `nginx_ecr_url`: Target repositories for your Docker images.
