variable "name_prefix" { type = string }
variable "region" { type = string }

# Cluster / discovery (from the ecs module)
variable "cluster_id" { type = string }
variable "cluster_name" { type = string }
variable "namespace_arn" { type = string }
variable "clickhouse_capacity_provider_name" { type = string }

# Networking
variable "private_subnet_ids" { type = list(string) }

# Security groups (from the security module)
variable "langfuse_sg_id" { type = string }
variable "clickhouse_sg_id" { type = string }
variable "langfuse_rds_sg_id" { type = string }

# Cross-cutting
variable "integration_secret_arn" { type = string }
variable "valkey_address" { type = string }
variable "valkey_port" { type = string }
variable "templates_dir" { type = string }

# Images
variable "clickhouse_image_url" {
  type    = string
  default = "clickhouse/clickhouse-server:24.8"
}
variable "langfuse_web_image_url" {
  type    = string
  default = "langfuse/langfuse:3"
}
variable "langfuse_worker_image_url" {
  type    = string
  default = "langfuse/langfuse-worker:3"
}

# Sizing (defaults match /docs)
variable "langfuse_rds_instance_class" {
  type    = string
  default = "db.t4g.small"
}
variable "clickhouse_ebs_size_gb" {
  type    = number
  default = 50
}
variable "clickhouse_ebs_iops" {
  type    = number
  default = 3000
}
