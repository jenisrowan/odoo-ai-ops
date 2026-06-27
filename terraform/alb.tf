# We want an application load balancer that are in 2 public subnets
resource "aws_lb" "main" {
  name = "odoo-alb"

  load_balancer_type = "application"
  security_groups = [
    aws_security_group.alb_http_sg.id,
    aws_security_group.alb_https_sg.id
  ]

  subnets = [aws_subnet.public_a.id, aws_subnet.public_b.id]
}

# We don't want sticky session here - Sticky session feels like a temperory fix than a permanent solution
# We are using a more robust mechanism called redis session store than EFS for session storage.
resource "aws_lb_target_group" "odoo" {
  port                 = 80
  protocol             = "HTTP"
  vpc_id               = aws_vpc.main.id
  target_type          = "ip"
  deregistration_delay = 60

  # Odoo's root path (/) redirects to /web/login (302), which ALB would treat
  # as unhealthy. Odoo's /web/health built-in endpoint that always returns 200.
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

  # Forward traffic only when X-Odoo-Origin-Verify header value matches the
  # value from cloudfront
  condition {
    http_header {
      http_header_name = "X-Odoo-Origin-Verify"
      values           = [random_password.cf_secret.result]
    }
  }
}
