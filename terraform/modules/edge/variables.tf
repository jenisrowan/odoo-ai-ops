variable "name_prefix" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "alb_http_sg_id" { type = string }
variable "alb_https_sg_id" { type = string }

variable "webhook_api_host" {
  description = "API Gateway host (no scheme) used as the CloudFront webhook origin."
  type        = string
}

variable "alb_origin_domain_name" {
  description = <<-EOT
    Optional custom domain (e.g. origin.example.com) that resolves to the ALB
    and is covered by alb_acm_certificate_arn. When both are set, CloudFront
    connects to the origin over HTTPS (TLS listener on the ALB, https-only
    origin policy). Left empty, the origin stays HTTP: ACM cannot issue
    certificates for the default *.elb.amazonaws.com name, so a domainless
    deployment cannot terminate TLS at the ALB - the CloudFront prefix-list
    security group plus the X-Odoo-Origin-Verify secret header remain the
    origin protections in that mode.
  EOT
  type        = string
  default     = ""
}

variable "alb_acm_certificate_arn" {
  description = "ACM certificate ARN for the ALB HTTPS listener; must cover alb_origin_domain_name."
  type        = string
  default     = ""
}
