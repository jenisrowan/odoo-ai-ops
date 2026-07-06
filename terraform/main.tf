# Root composition. Each concern is a module under ./modules; this file wires
# their inputs/outputs together. See moved.tf for safe state migration from the
# previous flat layout.

module "network" {
  source = "./modules/network"

  name_prefix        = local.name_prefix
  region             = var.region
  interface_services = local.interface_services
}

module "security" {
  source = "./modules/security"

  name_prefix          = local.name_prefix
  vpc_id               = module.network.vpc_id
  vpc_cidr             = module.network.vpc_cidr
  private_subnet_cidrs = module.network.private_subnet_cidrs
}

module "data" {
  source = "./modules/data"

  name_prefix         = local.name_prefix
  private_subnet_ids  = module.network.private_subnet_ids
  private_subnet_a_id = module.network.private_subnet_a_id
  private_subnet_b_id = module.network.private_subnet_b_id
  rds_sg_id           = module.security.rds_sg_id
  efs_sg_id           = module.security.efs_sg_id
  valkey_sg_id        = module.security.valkey_sg_id
}

module "ecr" {
  source = "./modules/ecr"
}

module "iam" {
  source = "./modules/iam"

  name_prefix            = local.name_prefix
  db_master_secret_arn   = module.data.db_master_secret_arn
  admin_secret_arn       = data.aws_secretsmanager_secret.odoo_admin_passwd.arn
  integration_secret_arn = data.aws_secretsmanager_secret.odoo_integration_credentials.arn
  efs_arn                = module.data.efs_arn
}

module "edge" {
  source = "./modules/edge"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us-east-1
  }

  name_prefix       = local.name_prefix
  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  alb_http_sg_id    = module.security.alb_http_sg_id
  alb_https_sg_id   = module.security.alb_https_sg_id
  # Webhooks enter through the same edge (WAF + CloudFront) -> API Gateway.
  webhook_api_host = module.webhooks.api_host
}

module "webhooks" {
  source = "./modules/webhooks"

  name_prefix            = local.name_prefix
  region                 = var.region
  account_id             = data.aws_caller_identity.current.account_id
  integration_secret_arn = data.aws_secretsmanager_secret.odoo_integration_credentials.arn
}

module "ecs" {
  source = "./modules/ecs"

  name_prefix   = local.name_prefix
  region        = var.region
  templates_dir = "${path.root}/../templates"

  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids

  ecs_node_sg_id  = module.security.ecs_node_sg_id
  ecs_task_sg_id  = module.security.ecs_task_sg_id
  fastapi_sg_id   = module.security.fastapi_sg_id
  pgbouncer_sg_id = module.security.pgbouncer_sg_id

  execution_role_arn    = module.iam.execution_role_arn
  instance_profile_name = module.iam.instance_profile_name
  odoo_task_role_arn    = module.iam.odoo_task_role_arn

  target_group_arn = module.edge.target_group_arn

  odoo_image_url    = var.odoo_image_url
  nginx_image_url   = var.nginx_image_url
  fastapi_image_url = var.fastapi_image_url

  db_master_secret_arn   = module.data.db_master_secret_arn
  admin_secret_arn       = data.aws_secretsmanager_secret.odoo_admin_passwd.arn
  integration_secret_arn = data.aws_secretsmanager_secret.odoo_integration_credentials.arn

  db_address          = module.data.db_address
  efs_id              = module.data.efs_id
  efs_access_point_id = module.data.efs_access_point_id
  valkey_address      = module.data.valkey_address
  valkey_port         = module.data.valkey_port

  sqs_queue_url = module.webhooks.queue_url
  sqs_queue_arn = module.webhooks.queue_arn

  # Service Connect name of the Langfuse server (literal to avoid an
  # ecs <-> telemetry dependency cycle; telemetry consumes ecs cluster outputs).
  langfuse_host       = "http://langfuse:3000"
  model_medium        = var.model_medium
  model_high          = var.model_high
  odoo_db_name        = var.odoo_db_name
  odoo_agent_username = var.odoo_agent_username
}

module "telemetry" {
  source = "./modules/telemetry"

  name_prefix   = local.name_prefix
  region        = var.region
  templates_dir = "${path.root}/../templates"

  cluster_id                        = module.ecs.cluster_id
  cluster_name                      = module.ecs.cluster_name
  namespace_arn                     = module.ecs.namespace_arn
  clickhouse_capacity_provider_name = module.ecs.clickhouse_capacity_provider_name

  private_subnet_ids = module.network.private_subnet_ids

  langfuse_sg_id     = module.security.langfuse_sg_id
  clickhouse_sg_id   = module.security.clickhouse_sg_id
  langfuse_rds_sg_id = module.security.langfuse_rds_sg_id

  integration_secret_arn = data.aws_secretsmanager_secret.odoo_integration_credentials.arn
  valkey_address         = module.data.valkey_address
  valkey_port            = module.data.valkey_port

  clickhouse_image_url      = var.clickhouse_image_url
  langfuse_web_image_url    = var.langfuse_web_image_url
  langfuse_worker_image_url = var.langfuse_worker_image_url
}

module "observability" {
  source = "./modules/observability"

  name_prefix = local.name_prefix
  alarm_email = var.alarm_email

  cluster_name       = module.ecs.cluster_name
  monitored_services = [module.ecs.odoo_service_name, module.ecs.fastapi_service_name]

  webhook_queue_name           = module.webhooks.queue_name
  webhook_dlq_name             = module.webhooks.dlq_name
  webhook_lambda_function_name = module.webhooks.lambda_function_name
}
