terraform {
  # Pessimistic pin: 1.6 is the floor (native `terraform test`, which
  # tests/*.tftest.hcl require, went GA in 1.6); `~>` caps below the next major
  # so an unvetted 2.0 can never be silently accepted.
  required_version = "~> 1.6"

  # Providers are exact-pinned here, and .terraform.lock.hcl (committed) records
  # the matching checksums for every dev/CI platform — together they make every
  # init reproducible and tamper-evident.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.53.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "2.8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.9.0"
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
