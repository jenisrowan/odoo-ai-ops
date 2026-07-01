# Odoo 19 + Nginx sidecar service (ALB routes to the nginx container on :80).

resource "aws_launch_template" "ecs" {
  name_prefix   = "${var.name_prefix}-ecs-template-"
  image_id      = data.aws_ssm_parameter.ecs_optimized_ami_arm64.value
  instance_type = var.odoo_instance_type

  update_default_version = true
  vpc_security_group_ids = [var.ecs_node_sg_id]

  iam_instance_profile {
    name = var.instance_profile_name
  }

  # Reserve CPU/memory for the OS and Docker daemon.
  user_data = base64encode(<<EOF
#!/bin/bash
echo ECS_CLUSTER=${aws_ecs_cluster.odoo.name} >> /etc/ecs/ecs.config
echo ECS_RESERVED_CPU=256 >> /etc/ecs/ecs.config
echo ECS_RESERVED_MEMORY=512 >> /etc/ecs/ecs.config
EOF
  )
}

resource "aws_autoscaling_group" "ecs_asg" {
  name_prefix           = "${var.name_prefix}-asg-"
  vpc_zone_identifier   = var.private_subnet_ids
  min_size              = 1
  max_size              = 4
  desired_capacity      = 1
  protect_from_scale_in = true

  lifecycle {
    create_before_destroy = true
  }

  launch_template {
    id      = aws_launch_template.ecs.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "${var.name_prefix}-ecs-node"
    propagate_at_launch = true
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = ""
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "odoo" {
  name = "${var.name_prefix}-cp-${random_id.cp_suffix.hex}"

  lifecycle {
    create_before_destroy = true
  }

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.ecs_asg.arn
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

resource "aws_ecs_task_definition" "odoo" {
  family                   = "odoo"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"

  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.odoo_task_role_arn

  # Sized for c6g.xlarge (4 vCPU / 8 GiB): 4096 - 256 (OS) - 256 (buffer).
  cpu    = "3584"
  memory = "7040" # 8192 - 512 (OS) - 640 (buffer)

  # Odoo/Nginx images run on Graviton (arm64).
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = templatefile("${var.templates_dir}/odoo-task.json", {
    admin_password_arn     = var.admin_secret_arn
    db_password_arn        = var.db_master_secret_arn
    nginx_image_url        = var.nginx_image_url
    odoo_image_url         = var.odoo_image_url
    aws_region             = var.region
    redis_host             = var.valkey_address
    redis_port             = var.valkey_port
    integration_secret_arn = var.integration_secret_arn
    agent_base_url         = "http://fastapi:8000"
    shopify_api_version    = "2025-01"
  })

  volume {
    name = "odoo-efs"

    efs_volume_configuration {
      file_system_id     = var.efs_id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = var.efs_access_point_id
        iam             = "ENABLED"
      }
    }
  }
}

resource "aws_ecs_service" "odoo" {
  name            = "odoo-service"
  cluster         = aws_ecs_cluster.odoo.id
  task_definition = aws_ecs_task_definition.odoo.arn
  desired_count   = 1

  depends_on                        = [aws_ecs_service.pgbouncer, aws_ecs_cluster_capacity_providers.odoo]
  health_check_grace_period_seconds = 90
  enable_execute_command            = true

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.odoo.name
    weight            = 100
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [var.ecs_task_sg_id]
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_private_dns_namespace.odoo.arn
    service {
      port_name      = "odoo"
      discovery_name = "odoo"
      client_alias {
        port     = 8069
        dns_name = "odoo"
      }
    }
  }

  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = "nginx"
    container_port   = 80
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}

resource "aws_appautoscaling_target" "ecs_target" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.odoo.name}/${aws_ecs_service.odoo.name}"
  scalable_dimension = "ecs:service:DesiredCount"

  min_capacity = 1
  max_capacity = 4
}

resource "aws_appautoscaling_policy" "ecs_cpu" {
  name               = "cpu-scale"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs_target.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs_target.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs_target.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = 75
  }
}
