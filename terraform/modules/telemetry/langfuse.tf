# Langfuse server (web + worker) on ECS Fargate Spot for cost optimization.

resource "aws_cloudwatch_log_group" "langfuse" {
  name              = "/ecs/langfuse"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "langfuse" {
  family                   = "langfuse"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024" # 1 vCPU  (per docs)
  memory                   = "2048" # 2 GiB   (per docs)

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.langfuse_task.arn

  container_definitions = templatefile("${var.templates_dir}/langfuse-task.json", {
    langfuse_web_image_url    = var.langfuse_web_image_url
    langfuse_worker_image_url = var.langfuse_worker_image_url
    aws_region                = var.region
    telemetry_secret_arn      = aws_secretsmanager_secret.telemetry.arn
    integration_secret_arn    = var.integration_secret_arn
    events_bucket             = aws_s3_bucket.langfuse_events.bucket
    redis_connection_string   = "rediss://${var.valkey_address}:${var.valkey_port}"
  })
}

resource "aws_ecs_service" "langfuse" {
  name            = "langfuse-service"
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.langfuse.arn
  desired_count   = 1

  # Fargate Spot for cost; the agent buffers telemetry in Valkey so brief Spot
  # reclamations don't drop traces.
  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 100
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.langfuse_sg_id]
    assign_public_ip = false
  }

  service_connect_configuration {
    enabled   = true
    namespace = var.namespace_arn
    service {
      port_name      = "langfuse"
      discovery_name = "langfuse"
      client_alias {
        port     = 3000
        dns_name = "langfuse"
      }
    }
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}
