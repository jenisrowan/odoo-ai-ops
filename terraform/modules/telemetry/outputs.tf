output "langfuse_host" {
  description = "Service Connect URL the agent uses as LANGFUSE_HOST."
  value       = "http://langfuse:3000"
}

output "clickhouse_cold_bucket" { value = aws_s3_bucket.clickhouse_cold.bucket }
output "langfuse_events_bucket" { value = aws_s3_bucket.langfuse_events.bucket }
output "langfuse_rds_address" { value = aws_db_instance.langfuse.address }
