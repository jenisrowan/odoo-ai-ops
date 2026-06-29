data "aws_secretsmanager_secret" "odoo_admin_passwd" {
  name = "odoo/admin/password"
}
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_secretsmanager_secret" "tavily_api_key" {
  name = "tavily/api/key"
}
data "aws_secretsmanager_secret" "odoo_integration_credentials" {
  name = "odoo/integration/credentials"
}
