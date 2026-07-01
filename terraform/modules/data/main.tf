# Persistence layer: RDS Postgres (Multi-AZ), EFS filestore, ElastiCache Valkey.

# --- RDS ---
resource "aws_db_subnet_group" "rds" {
  name       = "${var.name_prefix}-db-subnet-group"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_parameter_group" "postgres16" {
  name   = "${var.name_prefix}-postgres16-params"
  family = "postgres16"

  parameter {
    name         = "max_connections"
    value        = "110"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "postgres" {
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  multi_az          = true
  allocated_storage = var.db_allocated_storage

  username = "odoo"
  # AWS-managed (rotatable) master password - never lands in tfstate.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.rds.name
  vpc_security_group_ids = [var.rds_sg_id]
  parameter_group_name   = aws_db_parameter_group.postgres16.name
  skip_final_snapshot    = true
}

# --- EFS ---
resource "aws_efs_file_system" "odoo" {
  creation_token   = "${var.name_prefix}-efs"
  performance_mode = "generalPurpose"
  throughput_mode  = "elastic"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }
  lifecycle_policy {
    transition_to_archive = "AFTER_90_DAYS"
  }
  lifecycle_policy {
    transition_to_primary_storage_class = "AFTER_1_ACCESS"
  }
}

resource "aws_efs_mount_target" "efs_mount" {
  file_system_id  = aws_efs_file_system.odoo.id
  subnet_id       = var.private_subnet_a_id
  security_groups = [var.efs_sg_id]
}

resource "aws_efs_mount_target" "efs_mount2" {
  file_system_id  = aws_efs_file_system.odoo.id
  subnet_id       = var.private_subnet_b_id
  security_groups = [var.efs_sg_id]
}

resource "aws_efs_access_point" "odoo" {
  file_system_id = aws_efs_file_system.odoo.id

  root_directory {
    path = "/odoo-data"
    creation_info {
      owner_uid   = 101
      owner_gid   = 101
      permissions = "0755"
    }
  }
}

# --- ElastiCache Serverless (Valkey) ---
resource "aws_elasticache_serverless_cache" "valkey" {
  name                 = "${var.name_prefix}-valkey-serverless"
  engine               = "valkey"
  major_engine_version = "8"
  subnet_ids           = var.private_subnet_ids
  security_group_ids   = [var.valkey_sg_id]

  cache_usage_limits {
    data_storage {
      maximum = var.valkey_max_storage_gb
      unit    = "GB"
    }
  }
}
