# ECS cluster, Service Connect namespace, shared AMIs, capacity-provider
# association, and log groups.

resource "aws_ecs_cluster" "odoo" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  service_connect_defaults {
    namespace = aws_service_discovery_private_dns_namespace.odoo.arn
  }
}

resource "aws_service_discovery_private_dns_namespace" "odoo" {
  name        = "odoo.local"
  description = "Service Connect discovery namespace"
  vpc         = var.vpc_id
}

# All compute is Graviton (c6g/m6g/r6g per the docs) -> arm64 ECS-optimized AMI.
data "aws_ssm_parameter" "ecs_optimized_ami_arm64" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
}

# Random suffix for unique capacity-provider naming during replacement.
resource "random_id" "cp_suffix" {
  byte_length = 4
}

resource "aws_ecs_cluster_capacity_providers" "odoo" {
  cluster_name = aws_ecs_cluster.odoo.name
  capacity_providers = [
    aws_ecs_capacity_provider.odoo.name,
    aws_ecs_capacity_provider.pgbouncer.name,
    aws_ecs_capacity_provider.fastapi.name,
    aws_ecs_capacity_provider.clickhouse.name,
    # Built-in Fargate providers for the Langfuse server (Fargate Spot).
    "FARGATE",
    "FARGATE_SPOT",
  ]

  default_capacity_provider_strategy {
    base              = 1
    weight            = 100
    capacity_provider = aws_ecs_capacity_provider.odoo.name
  }
}

resource "aws_cloudwatch_log_group" "odoo_logs" {
  name              = "/ecs/odoo"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "fastapi_logs" {
  name              = "/ecs/fastapi"
  retention_in_days = 7
}
