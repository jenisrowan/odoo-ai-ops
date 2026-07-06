# Native `terraform test` — APPLY mode (real integration test).
#
# WARNING: this ACTUALLY provisions the full stack in the target AWS account,
# runs the assertions, then tears it all down. It is opt-in only — run it
# deliberately via the manual "Terraform Test" workflow (mode = plan-and-apply);
# it is never triggered on push, and it will incur real (short-lived) AWS cost.
#
# Isolation: it applies under a distinct name_prefix so prefixed resources won't
# collide with a deployed "odoo" stack. NOTE the few fixed-name resources (ECR
# repos: odoo-custom/nginx-custom/fastapi-agent/clickhouse-custom) are NOT
# prefixed — run this in an account where those repos don't already exist (e.g.
# a clean/sandbox account), or the ECR creation will conflict.
#
# Prerequisites: valid AWS creds, the odoo/admin/password +
# odoo/integration/credentials secrets present, and the S3 state backend
# reachable (the test uses its own ephemeral state — it will not touch the real
# deployment's state).

variables {
  name_prefix = "odoo-tftest"
}

run "full_stack_apply_smoke" {
  command = apply

  # Invariants still hold after a real apply.
  assert {
    condition     = output.odoo_instance_type == "c6g.xlarge"
    error_message = "Odoo must run on c6g.xlarge after apply."
  }
  assert {
    condition     = output.rds_multi_az == true
    error_message = "Primary RDS must be Multi-AZ after apply."
  }
  assert {
    condition     = output.lambda_runtime == "python3.14"
    error_message = "Webhook Lambda runtime must be python3.14 after apply."
  }

  # Computed values are known post-apply.
  assert {
    condition     = can(regex("^https://sqs\\.", output.webhook_queue_url))
    error_message = "Webhook SQS queue URL should be a valid https SQS endpoint."
  }
  assert {
    condition     = can(regex("^arn:aws:sqs:", output.webhook_dlq_arn))
    error_message = "Webhook DLQ ARN should be a valid SQS ARN after apply."
  }
  assert {
    condition     = output.cloudfront_url != ""
    error_message = "CloudFront distribution domain should be set after apply."
  }
}
