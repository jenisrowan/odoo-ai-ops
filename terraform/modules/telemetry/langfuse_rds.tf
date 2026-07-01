# Dedicated Langfuse RDS (Postgres) - transactional config store, isolated from
# the primary Odoo database (per the architecture).

resource "aws_db_subnet_group" "langfuse" {
  name       = "${var.name_prefix}-langfuse-db-subnet-group"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "langfuse" {
  identifier     = "${var.name_prefix}-langfuse"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.langfuse_rds_instance_class

  allocated_storage = 20
  multi_az          = false

  db_name  = "langfuse"
  username = "langfuse"
  password = random_password.langfuse_db.result

  db_subnet_group_name   = aws_db_subnet_group.langfuse.name
  vpc_security_group_ids = [var.langfuse_rds_sg_id]
  skip_final_snapshot    = true
}
