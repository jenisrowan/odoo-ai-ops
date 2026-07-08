# FastAPI + LangGraph agent service on dedicated Graviton (m6g.large) capacity.

# --- Task role (least privilege): consume SQS + ECS Exec ---
data "aws_iam_policy_document" "fastapi_task_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fastapi_task_role" {
  name               = "${var.name_prefix}-fastapi-task-role"
  assume_role_policy = data.aws_iam_policy_document.fastapi_task_trust.json
}

resource "aws_iam_role_policy" "fastapi_sqs_policy" {
  name = "${var.name_prefix}-fastapi-sqs-policy"
  role = aws_iam_role.fastapi_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = var.sqs_queue_arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "fastapi_exec_policy" {
  name = "${var.name_prefix}-fastapi-exec-policy"
  role = aws_iam_role.fastapi_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel"
        ]
        Resource = "*"
      }
    ]
  })
}

# --- Capacity (Graviton) ---
resource "aws_launch_template" "fastapi" {
  name_prefix   = "${var.name_prefix}-fastapi-"
  image_id      = data.aws_ssm_parameter.ecs_optimized_ami_arm64.value
  instance_type = var.fastapi_instance_type

  update_default_version = true
  vpc_security_group_ids = [var.ecs_node_sg_id]

  iam_instance_profile {
    name = var.instance_profile_name
  }

  user_data = base64encode(<<EOF
#!/bin/bash
echo ECS_CLUSTER=${aws_ecs_cluster.odoo.name} >> /etc/ecs/ecs.config
echo ECS_RESERVED_CPU=256 >> /etc/ecs/ecs.config
echo ECS_RESERVED_MEMORY=512 >> /etc/ecs/ecs.config
EOF
  )
}

resource "aws_autoscaling_group" "fastapi_asg" {
  name_prefix           = "${var.name_prefix}-fastapi-asg-"
  vpc_zone_identifier   = var.private_subnet_ids
  min_size              = 1
  max_size              = 4
  desired_capacity      = 1
  protect_from_scale_in = true

  lifecycle {
    create_before_destroy = true
  }

  launch_template {
    id      = aws_launch_template.fastapi.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "${var.name_prefix}-fastapi-node"
    propagate_at_launch = true
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = ""
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "fastapi" {
  name = "${var.name_prefix}-fastapi-cp-${random_id.cp_suffix.hex}"

  lifecycle {
    create_before_destroy = true
  }

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.fastapi_asg.arn
    managed_termination_protection = "ENABLED"
    managed_draining               = "ENABLED"

    managed_scaling {
      maximum_scaling_step_size = 2
      minimum_scaling_step_size = 1
      status                    = "ENABLED"
      target_capacity           = 100
    }
  }
}

# --- Task definition & service ---
resource "aws_ecs_task_definition" "fastapi" {
  family                   = "fastapi"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = "1792"
  memory                   = "7168"

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  execution_role_arn = var.execution_role_arn
  task_role_arn      = aws_iam_role.fastapi_task_role.arn

  container_definitions = templatefile("${var.templates_dir}/fastapi-task.json", {
    fastapi_image_url      = var.fastapi_image_url
    aws_region             = var.region
    sqs_queue_url          = var.sqs_queue_url
    valkey_url             = "rediss://${var.valkey_address}:${var.valkey_port}"
    odoo_base_url          = "http://odoo:8069"
    odoo_db                = var.odoo_db_name
    odoo_username          = var.odoo_agent_username
    integration_secret_arn = var.integration_secret_arn
    langfuse_host          = var.langfuse_host
    slack_channel          = "#fraud-review"
    model_medium           = var.model_medium
    model_high             = var.model_high
  })
}

resource "aws_ecs_service" "fastapi" {
  name            = "fastapi-service"
  cluster         = aws_ecs_cluster.odoo.id
  task_definition = aws_ecs_task_definition.fastapi.arn
  desired_count   = 1

  depends_on             = [aws_ecs_cluster_capacity_providers.odoo]
  enable_execute_command = true

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.fastapi.name
    weight            = 100
  }

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200

  # Abort + roll back a failing rollout instead of cycling tasks indefinitely.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [var.fastapi_sg_id]
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_private_dns_namespace.odoo.arn
    service {
      port_name      = "fastapi"
      discovery_name = "fastapi"
      client_alias {
        port     = 8000
        dns_name = "fastapi"
      }
    }
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}

resource "aws_appautoscaling_target" "fastapi_target" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.odoo.name}/${aws_ecs_service.fastapi.name}"
  scalable_dimension = "ecs:service:DesiredCount"

  min_capacity = 1
  max_capacity = 4
}

resource "aws_appautoscaling_policy" "fastapi_cpu" {
  name               = "${var.name_prefix}-fastapi-cpu-scale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.fastapi_target.resource_id
  scalable_dimension = aws_appautoscaling_target.fastapi_target.scalable_dimension
  service_namespace  = aws_appautoscaling_target.fastapi_target.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = 70
  }
}
