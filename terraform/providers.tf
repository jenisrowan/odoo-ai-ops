provider "aws" {
  region = var.region

  default_tags {
    tags = local.common_tags
  }
}

# WAF for CloudFront must be created in us-east-1.
provider "aws" {
  alias  = "us-east-1"
  region = "us-east-1"

  default_tags {
    tags = local.common_tags
  }
}
