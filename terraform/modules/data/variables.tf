variable "name_prefix" { type = string }
variable "private_subnet_ids" { type = list(string) }

variable "rds_sg_id" { type = string }
variable "efs_sg_id" { type = string }
variable "valkey_sg_id" { type = string }

variable "private_subnet_a_id" { type = string }
variable "private_subnet_b_id" { type = string }

variable "db_instance_class" {
  # Matches the docs' primary Odoo database instance.
  type    = string
  default = "db.m6g.xlarge"
}
variable "db_engine_version" {
  type    = string
  default = "16"
}
variable "db_allocated_storage" {
  type    = number
  default = 20
}
variable "valkey_max_storage_gb" {
  type    = number
  default = 10
}
