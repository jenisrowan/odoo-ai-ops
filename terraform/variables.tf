variable "region" {
  default = "ap-south-1"
}

variable "nginx_image_url" {
  description = "The full ECR URL and tag for the custom Nginx image"
  type        = string
  default     = "nginx:1.26-alpine"
}

variable "odoo_image_url" {
  description = "The full ECR URL and tag for the custom Odoo image"
  type        = string
  default     = "odoo:19.0"
}
