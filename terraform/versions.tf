terraform {
  # Pessimistic pin: 1.10 is the floor (S3-native state locking via
  # `use_lockfile` in the backend below landed in 1.10; native `terraform test`,
  # which tests/*.tftest.hcl require, went GA earlier in 1.6); `~>` caps below
  # the next major so an unvetted 2.0 can never be silently accepted.
  required_version = "~> 1.10"

  # Providers are exact-pinned here, and .terraform.lock.hcl (committed) records
  # the matching checksums for every dev/CI platform - together they make every
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

  # State locking is S3-native (a `.tflock` object written beside the state via
  # a conditional PUT), so there is no DynamoDB table to keep alive or pay for.
  backend "s3" {
    bucket       = "odoo-aws-cloud-s3"
    key          = "odoo-prod/terraform.tfstate"
    region       = "ap-south-1"
    use_lockfile = true
    encrypt      = true
  }
}
