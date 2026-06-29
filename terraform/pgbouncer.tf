
# PgBouncer Task Definition
resource "aws_ecs_task_definition" "pgbouncer" {
  family                   = "pgbouncer"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = "1280"
  memory                   = "512"

  execution_role_arn = aws_iam_role.ecs_task_execution_role.arn
  task_role_arn      = aws_iam_role.ecs_task_role.arn

  container_definitions = templatefile("${path.module}/../templates/pgbouncer-task.json", {
    db_host         = aws_db_instance.postgres.address
    db_password_arn = aws_db_instance.postgres.master_user_secret[0].secret_arn
    aws_region      = var.region
  })
}

# PgBouncer ECS Service
resource "aws_ecs_service" "pgbouncer" {
  name            = "pgbouncer-service"
  cluster         = aws_ecs_cluster.odoo.id
  task_definition = aws_ecs_task_definition.pgbouncer.arn
  desired_count   = 2

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.pgbouncer.name
    weight            = 100
  }

  network_configuration {
    subnets         = [aws_subnet.private_a.id, aws_subnet.private_b.id]
    security_groups = [aws_security_group.pgbouncer_sg.id]
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

# --- Dedicated PgBouncer Infrastructure ---
resource "aws_launch_template" "pgbouncer" {
  name_prefix   = "pgbouncer-template-"
  image_id      = data.aws_ssm_parameter.ecs_optimized_ami.value
  instance_type = "t3.micro"

  update_default_version = true
  vpc_security_group_ids = [aws_security_group.ecs_node_sg.id]

  iam_instance_profile {
    name = aws_iam_instance_profile.ecs_instance_profile.name
  }

  user_data = base64encode(<<EOF
#!/bin/bash
echo ECS_CLUSTER=${aws_ecs_cluster.odoo.name} >> /etc/ecs/ecs.config
EOF
  )
}

resource "aws_autoscaling_group" "pgbouncer_asg" {
  vpc_zone_identifier   = [aws_subnet.private_a.id, aws_subnet.private_b.id]
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
