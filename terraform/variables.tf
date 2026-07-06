variable "region" {
  description = "AWS region for the primary deployment."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Project identifier used for naming and tagging."
  type        = string
  default     = "odoo-ai-ops"
}

variable "name_prefix" {
  description = "Resource name prefix. Keep 'odoo' for the real deployment; override (e.g. in terraform test) to avoid name collisions."
  type        = string
  default     = "odoo"
}

variable "alarm_email" {
  description = "Email address to receive CloudWatch alarm notifications (DLQ, Lambda errors, service-down, …). Leave empty to create the alarms/SNS topic without an email subscription."
  type        = string
  default     = ""
}

variable "environment" {
  description = "Deployment environment (e.g. prod, staging)."
  type        = string
  default     = "prod"
}

variable "extra_tags" {
  description = "Additional tags merged into the default tag set."
  type        = map(string)
  default     = {}
}

variable "nginx_image_url" {
  description = "The full ECR URL and tag for the custom Nginx image"
  type        = string
  default     = "nginx:1.26-alpine"
}

variable "odoo_image_url" {
  description = "The full ECR URL and tag for the custom Odoo image"
  type        = string
  default     = "odoo:19.0"
}

variable "fastapi_image_url" {
  description = "The full ECR URL and tag for the FastAPI + LangGraph agent image"
  type        = string
  default     = "public.ecr.aws/docker/library/python:3.12-slim"
}

variable "clickhouse_image_url" {
  description = "ClickHouse image (custom build with S3 tiered-storage config)."
  type        = string
  default     = "clickhouse/clickhouse-server:24.8"
}

variable "langfuse_web_image_url" {
  description = "Langfuse web server image."
  type        = string
  default     = "langfuse/langfuse:3"
}

variable "langfuse_worker_image_url" {
  description = "Langfuse worker image."
  type        = string
  default     = "langfuse/langfuse-worker:3"
}

variable "odoo_db_name" {
  description = "Odoo database name the agent authenticates against over JSON-RPC."
  type        = string
  default     = "odoo"
}

variable "odoo_agent_username" {
  description = "Odoo login the FastAPI agent uses for JSON-RPC (must hold the AI Ops Agent group)."
  type        = string
  default     = "ai_ops_agent"
}

variable "langfuse_host" {
  description = "Base URL of the self-hosted Langfuse server (Service Connect / internal ALB)."
  type        = string
  default     = ""
}

variable "integration_secret_name" {
  description = "Secrets Manager secret holding the integration credentials JSON (Shopify/Slack/Anthropic/Langfuse/shared token)."
  type        = string
  default     = "odoo/integration/credentials"
}

variable "model_medium" {
  description = "Anthropic model id used for medium-risk (fast/cheap) screening."
  type        = string
  default     = "claude-haiku-4-5-20251001"
}

variable "model_high" {
  description = "Anthropic model id used for high-risk deep analysis."
  type        = string
  default     = "claude-sonnet-5"
}
