terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.36.0"
    }
    opensearch = {
      source  = "opensearch-project/opensearch"
      version = "2.3.2"
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

provider "aws" {
  region = var.region
}

# WAF for CloudFront must be in us-east-1
provider "aws" {
  alias  = "us-east-1"
  region = "us-east-1"
}

provider "opensearch" {
  url         = aws_opensearchserverless_collection.bedrock_kb.collection_endpoint
  aws_region  = var.region
  healthcheck = false
}
