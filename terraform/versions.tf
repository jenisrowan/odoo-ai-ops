terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.36.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  backend "s3" {
    bucket         = "odoo-aws-cloud-s3"
    key            = "odoo-prod/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "odoo-terraform-state-locks"
    encrypt        = true
  }
}
