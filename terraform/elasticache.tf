# We will use Valkey instead of Redis
# AWS promotion:
# You can save up to 33% with Serverless and 20% with node-based ElastiCache by choosing Valkey.
# Valkey is open source, established under the Linux Foundation, and fully compatible with Redis OSS v7.0.
resource "aws_elasticache_serverless_cache" "valkey" {
  name                 = "odoo-valkey-serverless"
  engine               = "valkey"
  major_engine_version = "8" # Valkey 8.2 is the default for engine "valkey" major "8"
  subnet_ids           = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  security_group_ids   = [aws_security_group.valkey_sg.id]

  cache_usage_limits {
    data_storage {
      maximum = 10
      unit    = "GB"
    }
  }
}
