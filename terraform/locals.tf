locals {
  # Resource name prefix. Defaults to "odoo" so the modular refactor preserves
  # the identities of pre-existing resources (see moved.tf). Overridable via
  # var.name_prefix so `terraform test` can apply under a distinct prefix and
  # avoid colliding with a deployed stack.
  name_prefix = var.name_prefix

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
