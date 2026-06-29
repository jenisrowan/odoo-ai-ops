# Public URL of the CloudFront distribution - the primary entry point for users
output "cloudfront_url" {
  value = aws_cloudfront_distribution.odoo.domain_name
}

# DNS name of the ALB - useful for internal testing before CloudFront is configured
output "alb_url" {
  value = aws_lb.main.dns_name
}

# ECR repository URL for the custom Odoo image - used by the CI/CD pipeline to push images
output "odoo_ecr_url" {
  value = aws_ecr_repository.odoo.repository_url
}

# ECR repository URL for the custom Nginx image - used by the CI/CD pipeline to push images
output "nginx_ecr_url" {
  value = aws_ecr_repository.nginx.repository_url
}
