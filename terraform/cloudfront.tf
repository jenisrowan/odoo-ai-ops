resource "random_password" "cf_secret" {
  length  = 32
  special = false
}

# Grab AWS Managed Policies for the default Odoo app route
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

# Custom Cache Policy for Odoo Assets (CSS/JS)
resource "aws_cloudfront_cache_policy" "odoo_assets" {
  name        = "odoo-assets-cache-policy"
  default_ttl = 3600
  max_ttl     = 86400
  min_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      # Odoo uses query strings for asset cache busting (e.g. ?v=123)
      query_string_behavior = "all"
    }
  }
}

# Custom Cache Policy for Core Static Files
resource "aws_cloudfront_cache_policy" "odoo_static" {
  name        = "odoo-static-cache-policy"
  default_ttl = 86400
  max_ttl     = 31536000
  min_ttl     = 3600

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

# 4. Custom Origin Request Policy to forward the Host header
# Odoo needs the Host header to know which database to serve, 
resource "aws_cloudfront_origin_request_policy" "odoo_forward_host" {
  name = "odoo-forward-host-only"

  cookies_config {
    cookie_behavior = "all"
  }
  headers_config {
    header_behavior = "whitelist"
    headers {
      items = ["Host"]
    }
  }
  query_strings_config {
    query_string_behavior = "all"
  }
}

# 5. Your updated CloudFront Distribution
resource "aws_cloudfront_distribution" "odoo" {
  origin {
    domain_name = aws_lb.main.dns_name
    origin_id   = "alb-origin"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }

    custom_header {
      name  = "X-Odoo-Origin-Verify"
      value = random_password.cf_secret.result
    }
  }

  enabled    = true
  web_acl_id = aws_wafv2_web_acl.odoo.arn

  # Default behavior: No caching, forward everything to Odoo
  default_cache_behavior {
    target_origin_id       = "alb-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  # Assets caching behavior
  ordered_cache_behavior {
    path_pattern           = "/web/assets/*"
    target_origin_id       = "alb-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = aws_cloudfront_cache_policy.odoo_assets.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.odoo_forward_host.id
  }


  # Image caching behavior (App icons, etc.)
  ordered_cache_behavior {
    path_pattern           = "/web/image/*"
    target_origin_id       = "alb-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = aws_cloudfront_cache_policy.odoo_static.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.odoo_forward_host.id
  }

  # Module-specific static files caching behavior
  ordered_cache_behavior {
    path_pattern           = "/*/static/*"
    target_origin_id       = "alb-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = aws_cloudfront_cache_policy.odoo_static.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.odoo_forward_host.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
