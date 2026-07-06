# S3 buckets: ClickHouse cold tier + Langfuse event/media blob storage.
# Both are reached privately through the free S3 Gateway VPC endpoint.

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# --- ClickHouse cold-storage tier ---
resource "aws_s3_bucket" "clickhouse_cold" {
  bucket = "${var.name_prefix}-clickhouse-cold-${random_id.bucket_suffix.hex}"
  # Allow `terraform destroy` to delete the bucket even with objects in it
  # (ClickHouse writes cold partitions here) so teardown never blocks.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "clickhouse_cold" {
  bucket                  = aws_s3_bucket.clickhouse_cold.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "clickhouse_cold" {
  bucket = aws_s3_bucket.clickhouse_cold.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# --- Langfuse event/media blob storage ---
resource "aws_s3_bucket" "langfuse_events" {
  bucket        = "${var.name_prefix}-langfuse-events-${random_id.bucket_suffix.hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "langfuse_events" {
  bucket                  = aws_s3_bucket.langfuse_events.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "langfuse_events" {
  bucket = aws_s3_bucket.langfuse_events.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
