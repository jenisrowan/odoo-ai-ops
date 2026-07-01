# ClickHouse analytical node capacity (r6g.xlarge Graviton). The ClickHouse
# task/service, EBS volume and S3 tiering live in the telemetry module; the
# capacity provider is defined here so it can join the cluster association.

resource "aws_launch_template" "clickhouse" {
  name_prefix   = "${var.name_prefix}-clickhouse-"
  image_id      = data.aws_ssm_parameter.ecs_optimized_ami_arm64.value
  instance_type = var.clickhouse_instance_type

  update_default_version = true
  vpc_security_group_ids = [var.ecs_node_sg_id]

  iam_instance_profile {
    name = var.instance_profile_name
  }

  user_data = base64encode(<<EOF
#!/bin/bash
echo ECS_CLUSTER=${aws_ecs_cluster.odoo.name} >> /etc/ecs/ecs.config
echo ECS_RESERVED_CPU=256 >> /etc/ecs/ecs.config
echo ECS_RESERVED_MEMORY=1024 >> /etc/ecs/ecs.config
EOF
  )
}

resource "aws_autoscaling_group" "clickhouse_asg" {
  name_prefix           = "${var.name_prefix}-clickhouse-asg-"
  vpc_zone_identifier   = var.private_subnet_ids
  min_size              = 1
  max_size              = 1
  desired_capacity      = 1
  protect_from_scale_in = true

  lifecycle {
    create_before_destroy = true
  }

  launch_template {
    id      = aws_launch_template.clickhouse.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "${var.name_prefix}-clickhouse-node"
    propagate_at_launch = true
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = ""
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "clickhouse" {
  name = "${var.name_prefix}-clickhouse-cp-${random_id.cp_suffix.hex}"

  lifecycle {
    create_before_destroy = true
  }

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.clickhouse_asg.arn
    managed_termination_protection = "ENABLED"
    managed_draining               = "ENABLED"

    managed_scaling {
      maximum_scaling_step_size = 1
      minimum_scaling_step_size = 1
      status                    = "ENABLED"
      target_capacity           = 100
    }
  }
}
