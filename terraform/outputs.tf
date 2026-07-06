# Public URL of the CloudFront distribution - the primary entry point for users.
output "cloudfront_url" {
  value = module.edge.cloudfront_domain
}

# DNS name of the ALB - useful for internal testing before CloudFront DNS is set.
output "alb_url" {
  value = module.edge.alb_dns_name
}

# ECR repositories used by the CI/CD pipeline.
output "odoo_ecr_url" {
  value = module.ecr.odoo_repo_url
}

output "nginx_ecr_url" {
  value = module.ecr.nginx_repo_url
}

output "fastapi_ecr_url" {
  value = module.ecr.fastapi_repo_url
}

output "clickhouse_ecr_url" {
  value = module.ecr.clickhouse_repo_url
}

# Public webhook ingress (through the edge - WAF + CloudFront). Point Shopify
# (orders/risk) and Slack at:
#   https://<cloudfront_url>/webhooks/shopify
#   https://<cloudfront_url>/webhooks/slack
output "webhook_url" {
  value = "https://${module.edge.cloudfront_domain}/webhooks"
}

# Direct API Gateway invoke URL (origin; useful for testing/diagnostics).
output "api_gateway_webhook_url" {
  value = module.webhooks.api_gateway_invoke_url
}

# SQS queue the agent polls privately via the SQS interface VPC endpoint.
output "webhook_queue_url" {
  value = module.webhooks.queue_url
}

# --- Invariants surfaced for `terraform test` (known at plan time) ---
output "odoo_instance_type" { value = module.ecs.odoo_instance_type }
output "pgbouncer_instance_type" { value = module.ecs.pgbouncer_instance_type }
output "fastapi_instance_type" { value = module.ecs.fastapi_instance_type }
output "clickhouse_instance_type" { value = module.ecs.clickhouse_instance_type }
output "rds_instance_class" { value = module.data.db_instance_class }
output "rds_multi_az" { value = module.data.db_multi_az }
output "lambda_runtime" { value = module.webhooks.lambda_runtime }
output "webhook_dlq_arn" { value = module.webhooks.dlq_arn }

# SNS topic that all CloudWatch alarms publish to - subscribe here to get paged.
output "alerts_topic_arn" { value = module.observability.sns_topic_arn }
