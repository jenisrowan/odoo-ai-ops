# Container image registries for the custom images.

variable "force_delete" {
  description = "Allow deleting repos with images (testing convenience)."
  type        = bool
  default     = true
}

resource "aws_ecr_repository" "odoo" {
  name                 = "odoo-custom"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "nginx" {
  name                 = "nginx-custom"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "fastapi" {
  name                 = "fastapi-agent"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "clickhouse" {
  name                 = "clickhouse-custom"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = true
  }
}

output "odoo_repo_url" { value = aws_ecr_repository.odoo.repository_url }
output "nginx_repo_url" { value = aws_ecr_repository.nginx.repository_url }
output "fastapi_repo_url" { value = aws_ecr_repository.fastapi.repository_url }
output "clickhouse_repo_url" { value = aws_ecr_repository.clickhouse.repository_url }
