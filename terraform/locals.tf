locals {
  # Resource name prefix. Kept as "odoo" so the modular refactor preserves the
  # identities of pre-existing resources (see moved.tf for state migration).
  name_prefix = "odoo"

  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    },
    var.extra_tags,
  )

  # AWS services exposed via Interface VPC endpoints (PrivateLink). Includes SQS
  # so the agent polls the webhook queue without traversing the NAT Gateway.
  interface_services = [
    "ecs",
    "ecs-agent",
    "ecs-telemetry",
    "ecr.dkr",
    "ecr.api",
    "logs",
    "secretsmanager",
    "sqs",
    "autoscaling",
  ]
}
