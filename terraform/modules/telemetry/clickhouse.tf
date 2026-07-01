# ClickHouse analytical service: EC2 (r6g.xlarge, arm64), hot data on an
# ECS-managed io2 EBS volume, older partitions tiered to S3.

resource "aws_cloudwatch_log_group" "clickhouse" {
  name              = "/ecs/clickhouse"
  retention_in_days = 7
}

resource "aws_ecs_task_definition" "clickhouse" {
  family                   = "clickhouse"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = "3584"  # r6g.xlarge: 4 vCPU - reservations
  memory                   = "30720" # 32 GiB - OS/agent headroom

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.clickhouse_task.arn

  container_definitions = templatefile("${var.templates_dir}/clickhouse-task.json", {
    clickhouse_image_url = var.clickhouse_image_url
    aws_region           = var.region
    s3_cold_endpoint     = "https://${aws_s3_bucket.clickhouse_cold.bucket}.s3.${var.region}.amazonaws.com/clickhouse/"
    telemetry_secret_arn = aws_secretsmanager_secret.telemetry.arn
  })

  # Hot data volume - provisioned per task by ECS (native EBS integration).
  volume {
    name                = "clickhouse-data"
    configure_at_launch = true
  }
}

resource "aws_ecs_service" "clickhouse" {
  name            = "clickhouse-service"
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.clickhouse.arn
  desired_count   = 1

  enable_execute_command = true

  capacity_provider_strategy {
    capacity_provider = var.clickhouse_capacity_provider_name
    weight            = 100
  }

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [var.clickhouse_sg_id]
  }

  # ECS-managed io2 EBS volume for hot ClickHouse data.
  volume_configuration {
    name = "clickhouse-data"
    managed_ebs_volume {
      role_arn         = aws_iam_role.ecs_ebs_infra.arn
      size_in_gb       = var.clickhouse_ebs_size_gb
      volume_type      = "io2"
      iops             = var.clickhouse_ebs_iops
      file_system_type = "xfs"
      encrypted        = true
    }
  }

  service_connect_configuration {
    enabled   = true
    namespace = var.namespace_arn
    service {
      port_name      = "clickhouse-http"
      discovery_name = "clickhouse"
      client_alias {
        port     = 8123
        dns_name = "clickhouse"
      }
    }
    service {
      port_name      = "clickhouse-native"
      discovery_name = "clickhouse-native"
      client_alias {
        port     = 9000
        dns_name = "clickhouse"
      }
    }
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}
