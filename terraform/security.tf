# Users should access Nginx only through CloudFront
# Direct ALB access is blocked
# Security group for ALB (HTTP)
# Two security groups because AWS only allows 60 rules per security group
resource "aws_security_group" "alb_http_sg" {
  name        = "odoo-alb-http-sg"
  description = "Allow HTTP inbound traffic from CloudFront"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTP from CloudFront only"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Security group for ALB (HTTPS)
resource "aws_security_group" "alb_https_sg" {
  name        = "odoo-alb-https-sg"
  description = "Allow HTTPS inbound traffic from CloudFront"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTPS from CloudFront only"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Since we used "awsvpc" we don't want ingress traffic for our EC2 hosts
# Traffic goes straight to the ENIs of the tasks
resource "aws_security_group" "ecs_node_sg" {
  name        = "odoo-ecs-node-sg"
  description = "Security group for underlying EC2 hosts"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}



resource "aws_security_group" "ecs_task_sg" {
  name        = "odoo-ecs-task-sg"
  description = "Allow traffic from ALB directly to the Odoo task ENI"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Allow Nginx HTTP from ALB"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    security_groups = [
      aws_security_group.alb_http_sg.id,
      aws_security_group.alb_https_sg.id
    ]
  }



  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}


# Expose the default RDS postgres port to ECS Task ENI
resource "aws_security_group" "rds_sg" {
  name        = "odoo-rds-sg"
  description = "Allow Postgres traffic from ECS tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from ECS"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.pgbouncer_sg.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Allow NFS from ECS Task ENI
resource "aws_security_group" "efs_sg" {
  name        = "odoo-efs-sg"
  description = "Allow NFS traffic from ECS tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "NFS from ECS tasks"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    security_groups = [
      aws_security_group.ecs_task_sg.id,
      aws_security_group.ecs_node_sg.id
    ]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# PgBouncer Security Group
resource "aws_security_group" "pgbouncer_sg" {
  name        = "odoo-pgbouncer-sg"
  description = "Security group for PgBouncer tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Allow PgBouncer from Odoo tasks"
    from_port       = 6432
    to_port         = 6432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_task_sg.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Valkey Security Group
resource "aws_security_group" "valkey_sg" {
  name        = "odoo-valkey-sg"
  description = "Security group for ElastiCache Valkey"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Allow Valkey from Odoo tasks"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_task_sg.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "vpc_endpoints_sg" {
  name        = "odoo-vpc-endpoints-sg"
  description = "Security group for VPC Interface Endpoints"
  vpc_id      = aws_vpc.main.id

  # Allow HTTPS ingress from the entire VPC CIDR
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "Odoo VPC Endpoints SG"
  }
}
