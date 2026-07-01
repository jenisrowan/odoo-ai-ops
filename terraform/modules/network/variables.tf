variable "name_prefix" { type = string }
variable "region" { type = string }

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}
variable "public_subnet_a_cidr" {
  type    = string
  default = "10.0.1.0/24"
}
variable "public_subnet_b_cidr" {
  type    = string
  default = "10.0.4.0/24"
}
variable "private_subnet_a_cidr" {
  type    = string
  default = "10.0.2.0/24"
}
variable "private_subnet_b_cidr" {
  type    = string
  default = "10.0.3.0/24"
}

variable "interface_services" {
  description = "AWS service short-names to expose via Interface VPC endpoints."
  type        = list(string)
}
