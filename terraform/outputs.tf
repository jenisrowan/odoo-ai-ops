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
