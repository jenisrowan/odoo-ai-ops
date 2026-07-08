# PgBouncer connection-pooling cluster (discovered via pgbouncer.odoo.local).

resource "aws_ecs_task_definition" "pgbouncer" {
  family                   = "pgbouncer"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  # m6g.medium = 1 vCPU (1024 units) / 4 GiB - must stay under the vCPU cap.
  cpu    = "768"
  memory = "512"

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.odoo_task_role_arn

  container_definitions = templatefile("${var.templates_dir}/pgbouncer-task.json", {
    db_host         = var.db_address
    db_password_arn = var.db_master_secret_arn
    aws_region      = var.region
  })
}

resource "aws_ecs_service" "pgbouncer" {
  name            = "pgbouncer-service"
  cluster         = aws_ecs_cluster.odoo.id
  task_definition = aws_ecs_task_definition.pgbouncer.arn
  desired_count   = 2

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.pgbouncer.name
    weight            = 100
  }

  # Abort + roll back a failing rollout instead of cycling tasks indefinitely.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [var.pgbouncer_sg_id]
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_private_dns_namespace.odoo.arn
    service {
      port_name      = "pgbouncer"
      discovery_name = "pgbouncer"
      client_alias {
        port     = 6432
        dns_name = "pgbouncer.odoo.local"
      }
    }
  }
}

resource "aws_launch_template" "pgbouncer" {
  name_prefix   = "pgbouncer-template-"
  image_id      = data.aws_ssm_parameter.ecs_optimized_ami_arm64.value
  instance_type = var.pgbouncer_instance_type

  update_default_version = true
  vpc_security_group_ids = [var.ecs_node_sg_id]

  iam_instance_profile {
    name = var.instance_profile_name
  }

  user_data = base64encode(<<EOF
#!/bin/bash
echo ECS_CLUSTER=${aws_ecs_cluster.odoo.name} >> /etc/ecs/ecs.config
EOF
  )
}

resource "aws_autoscaling_group" "pgbouncer_asg" {
  vpc_zone_identifier   = var.private_subnet_ids
  min_size              = 2
  max_size              = 2
  desired_capacity      = 2
  protect_from_scale_in = true

  launch_template {
    id      = aws_launch_template.pgbouncer.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "pgbouncer-node"
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "pgbouncer" {
  name = "pgbouncer-capacity-provider"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.pgbouncer_asg.arn
    managed_termination_protection = "ENABLED"

    managed_scaling {
      maximum_scaling_step_size = 1
      minimum_scaling_step_size = 1
      status                    = "ENABLED"
      target_capacity           = 100
    }
  }
}
