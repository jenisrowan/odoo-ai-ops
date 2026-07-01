variable "name_prefix" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "alb_http_sg_id" { type = string }
variable "alb_https_sg_id" { type = string }

variable "webhook_api_host" {
  description = "API Gateway host (no scheme) used as the CloudFront webhook origin."
  type        = string
}
