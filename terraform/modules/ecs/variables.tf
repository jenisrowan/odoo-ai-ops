variable "name_prefix" { type = string }
variable "region" { type = string }

# Instance types - defaults match the exact Graviton families in /docs.
variable "odoo_instance_type" {
  type    = string
  default = "c6g.xlarge"
}
variable "pgbouncer_instance_type" {
  type    = string
  default = "m6g.medium"
}
variable "fastapi_instance_type" {
  type    = string
  default = "m6g.large"
}
variable "clickhouse_instance_type" {
  type    = string
  default = "r6g.xlarge"
}

# Networking
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }

# Security groups
variable "ecs_node_sg_id" { type = string }
variable "ecs_task_sg_id" { type = string }
variable "fastapi_sg_id" { type = string }
variable "pgbouncer_sg_id" { type = string }

# IAM (from iam module)
variable "execution_role_arn" { type = string }
variable "instance_profile_name" { type = string }
variable "odoo_task_role_arn" { type = string }

# Edge
variable "target_group_arn" { type = string }

# Images
variable "odoo_image_url" { type = string }
variable "nginx_image_url" { type = string }
variable "fastapi_image_url" { type = string }

# Secrets
variable "db_master_secret_arn" { type = string }
variable "admin_secret_arn" { type = string }
variable "integration_secret_arn" { type = string }

# Data layer
variable "db_address" { type = string }
variable "efs_id" { type = string }
variable "efs_access_point_id" { type = string }
variable "valkey_address" { type = string }
variable "valkey_port" { type = string }

# Webhooks (for the agent's SQS consume permissions)
variable "sqs_queue_url" { type = string }
variable "sqs_queue_arn" { type = string }

# Agent config
variable "langfuse_host" { type = string }
variable "model_medium" { type = string }
variable "model_high" { type = string }
variable "odoo_db_name" { type = string }
variable "odoo_agent_username" { type = string }

variable "templates_dir" {
  description = "Path to the ECS container-definition templates."
  type        = string
}
