# VPC Endpoints for private ECS/ECR connectivity
# These allow the ECS agent to communicate with the AWS control plane
# and pull images from ECR without requiring a NAT Gateway egress.

# --- Gateway Endpoints (Free) ---

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]

  tags = {
    Name = "odoo-s3-endpoint"
  }
}

# --- Interface Endpoints (Hourly Cost) ---

locals {
  interface_services = [
    "ecs",
    "ecs-agent",
    "ecs-telemetry",
    "ecr.dkr",
    "ecr.api",
    "logs",
    "secretsmanager",
    "bedrock-runtime",
    "autoscaling"
  ]
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(local.interface_services)

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  security_group_ids  = [aws_security_group.vpc_endpoints_sg.id]
  subnet_ids          = [aws_subnet.private_a.id, aws_subnet.private_b.id]

  tags = {
    Name = "odoo-${each.value}-endpoint"
  }
}
