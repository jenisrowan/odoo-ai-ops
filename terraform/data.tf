# Pre-existing secrets and account/region lookups consumed across modules.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# Master/admin password for Odoo (created manually before apply).
data "aws_secretsmanager_secret" "odoo_admin_passwd" {
  name = "odoo/admin/password"
}

# Integration credentials JSON (Shopify/Slack/Anthropic/Langfuse/shared token).
data "aws_secretsmanager_secret" "odoo_integration_credentials" {
  name = var.integration_secret_name
}
