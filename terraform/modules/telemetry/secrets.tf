# Generated runtime secrets for the telemetry stack. These are created by
# Terraform (so they land in state, which is encrypted in the S3 backend) rather
# than pre-provisioned, because they are internal service-to-service credentials
# - unlike the Odoo master password, which stays AWS-managed/zero-knowledge.

resource "random_password" "clickhouse" {
  length  = 24
  special = false
}

resource "random_password" "langfuse_db" {
  length  = 24
  special = false
}

resource "random_password" "nextauth" {
  length  = 40
  special = false
}

resource "random_password" "salt" {
  length  = 32
  special = false
}

# Langfuse ENCRYPTION_KEY must be 64 hex chars (32 bytes).
resource "random_id" "encryption_key" {
  byte_length = 32
}

resource "aws_secretsmanager_secret" "telemetry" {
  name        = "${var.name_prefix}-telemetry-runtime"
  description = "Runtime secrets for Langfuse + ClickHouse."
}

resource "aws_secretsmanager_secret_version" "telemetry" {
  secret_id = aws_secretsmanager_secret.telemetry.id
  secret_string = jsonencode({
    clickhouse_password = random_password.clickhouse.result
    nextauth_secret     = random_password.nextauth.result
    salt                = random_password.salt.result
    encryption_key      = random_id.encryption_key.hex
    database_url = format(
      "postgresql://langfuse:%s@%s:5432/langfuse",
      random_password.langfuse_db.result,
      aws_db_instance.langfuse.address,
    )
  })
}
