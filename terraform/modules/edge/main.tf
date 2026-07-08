# Edge: Application Load Balancer, CloudFront distribution, and global WAF.
# Direct ALB access is blocked; only CloudFront (which injects a shared secret
# header) may reach the origin.

locals {
  # CloudFront -> ALB over TLS requires a custom origin domain with a matching
  # ACM certificate (see the variable docs); with both provided the HTTPS
  # listener is created and the origin switches to https-only.
  https_origin = var.alb_origin_domain_name != "" && var.alb_acm_certificate_arn != ""
}

resource "random_password" "cf_secret" {
  length  = 32
  special = false
}

# --- Application Load Balancer ---
resource "aws_lb" "main" {
  name               = "${var.name_prefix}-alb"
  load_balancer_type = "application"
  security_groups    = [var.alb_http_sg_id, var.alb_https_sg_id]
  subnets            = var.public_subnet_ids
}

resource "aws_lb_target_group" "odoo" {
  port                 = 80
  protocol             = "HTTP"
  vpc_id               = var.vpc_id
  target_type          = "ip"
  deregistration_delay = 60

  health_check {
    path                = "/web/health"
    protocol            = "HTTP"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Access Denied - Please use the official CloudFront URL"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "allow_cloudfront_secret" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.odoo.arn
  }

  condition {
    http_header {
      http_header_name = "X-Odoo-Origin-Verify"
      values           = [random_password.cf_secret.result]
    }
  }
}

# HTTPS listener - only when a custom origin domain + ACM certificate are
# provided (see variables). Same deny-by-default + origin-secret rule as HTTP.
resource "aws_lb_listener" "https" {
  count = local.https_origin ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.alb_acm_certificate_arn

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Access Denied - Please use the official CloudFront URL"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "allow_cloudfront_secret_https" {
  count = local.https_origin ? 1 : 0

  listener_arn = aws_lb_listener.https[0].arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.odoo.arn
  }

  condition {
    http_header {
      http_header_name = "X-Odoo-Origin-Verify"
      values           = [random_password.cf_secret.result]
    }
  }
}

# --- CloudFront ---
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

resource "aws_cloudfront_cache_policy" "odoo_assets" {
  name        = "${var.name_prefix}-assets-cache-policy"
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
      query_string_behavior = "all"
    }
  }
}

resource "aws_cloudfront_cache_policy" "odoo_static" {
  name        = "${var.name_prefix}-static-cache-policy"
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

resource "aws_cloudfront_origin_request_policy" "odoo_forward_host" {
  name = "${var.name_prefix}-forward-host-only"

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

resource "aws_cloudfront_distribution" "odoo" {
  origin {
    # With a custom origin domain + certificate the CloudFront->ALB hop is TLS;
    # otherwise it falls back to the ALB's default DNS name over HTTP (see
    # alb_origin_domain_name variable docs for why).
    domain_name = local.https_origin ? var.alb_origin_domain_name : aws_lb.main.dns_name
    origin_id   = "alb-origin"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = local.https_origin ? "https-only" : "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }

    custom_header {
      name  = "X-Odoo-Origin-Verify"
      value = random_password.cf_secret.result
    }
  }

  # Webhook ingress origin: API Gateway (HTTP API). Per the architecture,
  # Shopify/Slack webhooks enter through the same edge (WAF + CloudFront) and
  # are routed to API Gateway -> Lambda -> SQS.
  origin {
    domain_name = var.webhook_api_host
    origin_id   = "apigw-origin"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 30
    }
  }

  enabled    = true
  web_acl_id = aws_wafv2_web_acl.odoo.arn

  # Webhooks: forward POSTs unaltered (caching disabled, all headers/body) so
  # the Lambda can validate the Shopify/Slack HMAC over the raw body.
  ordered_cache_behavior {
    path_pattern           = "/webhooks/*"
    target_origin_id       = "apigw-origin"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  default_cache_behavior {
    target_origin_id       = "alb-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  ordered_cache_behavior {
    path_pattern           = "/web/assets/*"
    target_origin_id       = "alb-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = aws_cloudfront_cache_policy.odoo_assets.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.odoo_forward_host.id
  }

  ordered_cache_behavior {
    path_pattern           = "/web/image/*"
    target_origin_id       = "alb-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = aws_cloudfront_cache_policy.odoo_static.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.odoo_forward_host.id
  }

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

# --- WAF (CLOUDFRONT scope must be us-east-1) ---
resource "aws_wafv2_web_acl" "odoo" {
  provider    = aws.us_east_1
  name        = "${var.name_prefix}-waf"
  description = "WAF for Odoo CloudFront distribution"
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "AWS-AWSManagedRulesAmazonIpReputationList"
    priority = 10

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "AWSManagedRulesAmazonIpReputationListMetric"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWS-AWSManagedRulesBotControlRuleSet"
    priority = 20

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesBotControlRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "AWSManagedRulesBotControlRuleSetMetric"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "IPRateLimit"
    priority = 30

    action {
      captcha {}
    }

    statement {
      rate_based_statement {
        limit              = 2200
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "IPRateLimitMetric"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "odoo-waf-metric"
    sampled_requests_enabled   = true
  }
}

output "alb_dns_name" { value = aws_lb.main.dns_name }
output "target_group_arn" { value = aws_lb_target_group.odoo.arn }
output "cloudfront_domain" { value = aws_cloudfront_distribution.odoo.domain_name }
