# ECR Repository for Custom Odoo Image
resource "aws_ecr_repository" "odoo" {
  name                 = "odoo-custom"
  image_tag_mutability = "MUTABLE"
  force_delete         = true # Only for testing

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ECR Repository for Custom Nginx Image
resource "aws_ecr_repository" "nginx" {
  name                 = "nginx-custom"
  image_tag_mutability = "MUTABLE"
  force_delete         = true # Only for testing

  image_scanning_configuration {
    scan_on_push = true
  }
}
