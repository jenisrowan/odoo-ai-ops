output "db_address" { value = aws_db_instance.postgres.address }
output "db_master_secret_arn" {
  value = aws_db_instance.postgres.master_user_secret[0].secret_arn
}

output "efs_id" { value = aws_efs_file_system.odoo.id }
output "efs_arn" { value = aws_efs_file_system.odoo.arn }
output "efs_access_point_id" { value = aws_efs_access_point.odoo.id }

output "valkey_address" {
  value = aws_elasticache_serverless_cache.valkey.endpoint[0].address
}
output "valkey_port" {
  value = aws_elasticache_serverless_cache.valkey.endpoint[0].port
}

# Exposed for terraform test assertions.
output "db_instance_class" { value = aws_db_instance.postgres.instance_class }
output "db_multi_az" { value = aws_db_instance.postgres.multi_az }
