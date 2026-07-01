# Application security groups.

# CloudFront origin-facing managed prefix list (for ALB ingress).
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

# ALB HTTP (CloudFront only). Split HTTP/HTTPS because AWS caps rules per SG.
resource "aws_security_group" "alb_http_sg" {
  name        = "${var.name_prefix}-alb-http-sg"
  description = "Allow HTTP inbound traffic from CloudFront"
  vpc_id      = var.vpc_id

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

resource "aws_security_group" "alb_https_sg" {
  name        = "${var.name_prefix}-alb-https-sg"
  description = "Allow HTTPS inbound traffic from CloudFront"
  vpc_id      = var.vpc_id

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

# Underlying EC2 hosts get no ingress (awsvpc -> traffic hits task ENIs).
resource "aws_security_group" "ecs_node_sg" {
  name        = "${var.name_prefix}-ecs-node-sg"
  description = "Security group for underlying EC2 hosts"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Odoo/Nginx task ENI.
resource "aws_security_group" "ecs_task_sg" {
  name        = "${var.name_prefix}-ecs-task-sg"
  description = "Allow traffic from ALB to the Nginx sidecar, and agent JSON-RPC to Odoo"
  vpc_id      = var.vpc_id

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

  # Odoo JSON-RPC/webhook port reached by the FastAPI agent via Service Connect.
  # Scoped to the private subnet CIDRs (agent ENIs) to avoid a mutual SG cycle.
  ingress {
    description = "Allow Odoo JSON-RPC/webhook from private subnets (FastAPI agent)"
    from_port   = 8069
    to_port     = 8069
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# FastAPI agent task ENI.
resource "aws_security_group" "fastapi_sg" {
  name        = "${var.name_prefix}-fastapi-sg"
  description = "Security group for the FastAPI + LangGraph agent tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Allow Odoo to start AI tasks (REST)"
    from_port       = 8000
    to_port         = 8000
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

# RDS (Postgres from PgBouncer only).
resource "aws_security_group" "rds_sg" {
  name        = "${var.name_prefix}-rds-sg"
  description = "Allow Postgres traffic from PgBouncer"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from PgBouncer"
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

# EFS (NFS from Odoo tasks/hosts).
resource "aws_security_group" "efs_sg" {
  name        = "${var.name_prefix}-efs-sg"
  description = "Allow NFS traffic from ECS tasks"
  vpc_id      = var.vpc_id

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

# PgBouncer.
resource "aws_security_group" "pgbouncer_sg" {
  name        = "${var.name_prefix}-pgbouncer-sg"
  description = "Security group for PgBouncer tasks"
  vpc_id      = var.vpc_id

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

# ElastiCache Serverless (Valkey): 6379 primary, 6380 reader.
resource "aws_security_group" "valkey_sg" {
  name        = "${var.name_prefix}-valkey-sg"
  description = "Security group for ElastiCache Valkey"
  vpc_id      = var.vpc_id

  ingress {
    description = "Allow Valkey from Odoo, FastAPI agent, and Langfuse tasks"
    from_port   = 6379
    to_port     = 6380
    protocol    = "tcp"
    security_groups = [
      aws_security_group.ecs_task_sg.id,
      aws_security_group.fastapi_sg.id,
      aws_security_group.langfuse_sg.id,
    ]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- Telemetry (Langfuse + ClickHouse) ---

# Langfuse server (ECS Fargate Spot). The agent flushes telemetry to it on :3000.
resource "aws_security_group" "langfuse_sg" {
  name        = "${var.name_prefix}-langfuse-sg"
  description = "Security group for the Langfuse server tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Langfuse web from the FastAPI agent"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.fastapi_sg.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ClickHouse analytical node. Reached by Langfuse on HTTP (8123) and native (9000).
resource "aws_security_group" "clickhouse_sg" {
  name        = "${var.name_prefix}-clickhouse-sg"
  description = "Security group for the ClickHouse tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "ClickHouse HTTP + native from Langfuse"
    from_port       = 8123
    to_port         = 8123
    protocol        = "tcp"
    security_groups = [aws_security_group.langfuse_sg.id]
  }

  ingress {
    description     = "ClickHouse native protocol from Langfuse"
    from_port       = 9000
    to_port         = 9000
    protocol        = "tcp"
    security_groups = [aws_security_group.langfuse_sg.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Dedicated Langfuse RDS (Postgres).
resource "aws_security_group" "langfuse_rds_sg" {
  name        = "${var.name_prefix}-langfuse-rds-sg"
  description = "Security group for the dedicated Langfuse RDS"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from Langfuse"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.langfuse_sg.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
