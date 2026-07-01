terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # WAF for CloudFront must be created in us-east-1.
      configuration_aliases = [aws.us_east_1]
    }
    random = { source = "hashicorp/random" }
  }
}
