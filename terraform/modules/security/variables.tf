variable "name_prefix" { type = string }
variable "vpc_id" { type = string }
variable "vpc_cidr" { type = string }

variable "private_subnet_cidrs" {
  description = "Private subnet CIDRs allowed to reach Odoo's JSON-RPC port (agent ENIs)."
  type        = list(string)
}
