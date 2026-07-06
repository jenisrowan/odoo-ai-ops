# Native `terraform test` — PLAN mode.
#
# Validates the doc-critical invariants that are known at plan time (Graviton
# instance families, RDS class + Multi-AZ, Lambda runtime). Uses the REAL AWS
# provider (no mocks), so it must run with valid AWS credentials and requires
# the odoo/admin/password + odoo/integration/credentials secrets to exist
# (data sources are read during plan). Run via the manual "Terraform Test"
# workflow. Nothing is created — `command = plan` only.

run "invariants_match_docs" {
  command = plan

  assert {
    condition     = output.odoo_instance_type == "c6g.xlarge"
    error_message = "Odoo must run on c6g.xlarge (per /docs)."
  }
  assert {
    condition     = output.pgbouncer_instance_type == "m6g.medium"
    error_message = "PgBouncer must run on m6g.medium (per /docs)."
  }
  assert {
    condition     = output.fastapi_instance_type == "m6g.large"
    error_message = "FastAPI agent must run on m6g.large (per /docs)."
  }
  assert {
    condition     = output.clickhouse_instance_type == "r6g.xlarge"
    error_message = "ClickHouse must run on r6g.xlarge (per /docs)."
  }
  assert {
    condition     = output.rds_instance_class == "db.m6g.xlarge"
    error_message = "Primary RDS must be db.m6g.xlarge (per /docs)."
  }
  assert {
    condition     = output.rds_multi_az == true
    error_message = "Primary RDS must be Multi-AZ."
  }
  assert {
    condition     = output.lambda_runtime == "python3.14"
    error_message = "Webhook Lambda must use the python3.14 runtime."
  }
}

# Prove the config is parameterised and the name_prefix override works (the
# apply test relies on it to avoid colliding with a deployed stack).
run "name_prefix_is_overridable" {
  command = plan

  variables {
    name_prefix = "odoo-test"
  }

  assert {
    condition     = output.odoo_instance_type == "c6g.xlarge"
    error_message = "Instance family must be unaffected by the name_prefix override."
  }
}
