resource "aws_db_subnet_group" "rds" {
  name       = "odoo-db-subnet-group"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]
}

resource "aws_db_instance" "postgres" {
  engine         = "postgres"
  engine_version = "16"
  instance_class = "db.t4g.micro"

  multi_az          = true
  allocated_storage = 20

  username = "odoo"

  # Let AWS autogenerate
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.rds.name
  vpc_security_group_ids = [aws_security_group.rds_sg.id]
  parameter_group_name   = aws_db_parameter_group.postgres16.name
  skip_final_snapshot    = true
}

# postgresql.conf equivalent
resource "aws_db_parameter_group" "postgres16" {
  name   = "odoo-postgres16-params"
  family = "postgres16"

  # Increase connections to handle 4 scaled Odoo tasks on micro RAM
  parameter {
    name         = "max_connections"
    value        = "110"
    apply_method = "pending-reboot"
  }
}
