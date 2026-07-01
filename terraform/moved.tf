# State migration: the configuration was refactored from a flat layout into
# ./modules. These `moved` blocks tell Terraform that each resource is the SAME
# object, just at a new address - so `terraform apply` migrates state in place
# instead of destroying and recreating. Resource names are unchanged, so the
# net plan after migration is a no-op.
#
# (Resources added during this change - SQS/Lambda/API Gateway/FastAPI - were
#  never applied under the flat layout, so their moved blocks are harmless
#  no-ops when the source address isn't present in state.)

# ----- network -----

moved {
  from = aws_vpc.main
  to   = module.network.aws_vpc.main
}
moved {
  from = aws_subnet.public_a
  to   = module.network.aws_subnet.public_a
}
moved {
  from = aws_subnet.public_b
  to   = module.network.aws_subnet.public_b
}
moved {
  from = aws_subnet.private_a
  to   = module.network.aws_subnet.private_a
}
moved {
  from = aws_subnet.private_b
  to   = module.network.aws_subnet.private_b
}
moved {
  from = aws_internet_gateway.main
  to   = module.network.aws_internet_gateway.main
}
moved {
  from = aws_route_table.public
  to   = module.network.aws_route_table.public
}
moved {
  from = aws_route_table.private
  to   = module.network.aws_route_table.private
}
moved {
  from = aws_route_table_association.public_a
  to   = module.network.aws_route_table_association.public_a
}
moved {
  from = aws_route_table_association.public_b
  to   = module.network.aws_route_table_association.public_b
}
moved {
  from = aws_route_table_association.private_a
  to   = module.network.aws_route_table_association.private_a
}
moved {
  from = aws_route_table_association.private_b
  to   = module.network.aws_route_table_association.private_b
}
moved {
  from = aws_nat_gateway.main
  to   = module.network.aws_nat_gateway.main
}
moved {
  from = aws_security_group.vpc_endpoints_sg
  to   = module.network.aws_security_group.vpc_endpoints_sg
}
moved {
  from = aws_vpc_endpoint.s3
  to   = module.network.aws_vpc_endpoint.s3
}
moved {
  from = aws_vpc_endpoint.interface
  to   = module.network.aws_vpc_endpoint.interface
}

# ----- security -----
moved {
  from = aws_security_group.alb_http_sg
  to   = module.security.aws_security_group.alb_http_sg
}
moved {
  from = aws_security_group.alb_https_sg
  to   = module.security.aws_security_group.alb_https_sg
}
moved {
  from = aws_security_group.ecs_node_sg
  to   = module.security.aws_security_group.ecs_node_sg
}
moved {
  from = aws_security_group.ecs_task_sg
  to   = module.security.aws_security_group.ecs_task_sg
}
moved {
  from = aws_security_group.fastapi_sg
  to   = module.security.aws_security_group.fastapi_sg
}
moved {
  from = aws_security_group.rds_sg
  to   = module.security.aws_security_group.rds_sg
}
moved {
  from = aws_security_group.efs_sg
  to   = module.security.aws_security_group.efs_sg
}
moved {
  from = aws_security_group.pgbouncer_sg
  to   = module.security.aws_security_group.pgbouncer_sg
}
moved {
  from = aws_security_group.valkey_sg
  to   = module.security.aws_security_group.valkey_sg
}

# ----- data (rds, efs, valkey) -----
moved {
  from = aws_db_subnet_group.rds
  to   = module.data.aws_db_subnet_group.rds
}
moved {
  from = aws_db_parameter_group.postgres16
  to   = module.data.aws_db_parameter_group.postgres16
}
moved {
  from = aws_db_instance.postgres
  to   = module.data.aws_db_instance.postgres
}
moved {
  from = aws_efs_file_system.odoo
  to   = module.data.aws_efs_file_system.odoo
}
moved {
  from = aws_efs_mount_target.efs_mount
  to   = module.data.aws_efs_mount_target.efs_mount
}
moved {
  from = aws_efs_mount_target.efs_mount2
  to   = module.data.aws_efs_mount_target.efs_mount2
}
moved {
  from = aws_efs_access_point.odoo
  to   = module.data.aws_efs_access_point.odoo
}
moved {
  from = aws_elasticache_serverless_cache.valkey
  to   = module.data.aws_elasticache_serverless_cache.valkey
}

# ----- ecr -----
moved {
  from = aws_ecr_repository.odoo
  to   = module.ecr.aws_ecr_repository.odoo
}
moved {
  from = aws_ecr_repository.nginx
  to   = module.ecr.aws_ecr_repository.nginx
}
moved {
  from = aws_ecr_repository.fastapi
  to   = module.ecr.aws_ecr_repository.fastapi
}

# ----- iam -----
moved {
  from = aws_iam_role.ecs_task_execution_role
  to   = module.iam.aws_iam_role.ecs_task_execution_role
}
moved {
  from = aws_iam_role_policy_attachment.ecs_task_execution_role_policy
  to   = module.iam.aws_iam_role_policy_attachment.ecs_task_execution_role_policy
}
moved {
  from = aws_iam_role_policy.ecs_secrets_policy
  to   = module.iam.aws_iam_role_policy.ecs_secrets_policy
}
moved {
  from = aws_iam_role.ecs_instance_role
  to   = module.iam.aws_iam_role.ecs_instance_role
}
moved {
  from = aws_iam_role_policy_attachment.ecs_instance_role_policy
  to   = module.iam.aws_iam_role_policy_attachment.ecs_instance_role_policy
}
moved {
  from = aws_iam_instance_profile.ecs_instance_profile
  to   = module.iam.aws_iam_instance_profile.ecs_instance_profile
}
moved {
  from = aws_iam_role.ecs_task_role
  to   = module.iam.aws_iam_role.ecs_task_role
}
moved {
  from = aws_iam_role_policy.ecs_efs_policy
  to   = module.iam.aws_iam_role_policy.ecs_efs_policy
}
moved {
  from = aws_iam_role_policy.ecs_task_exec_policy
  to   = module.iam.aws_iam_role_policy.ecs_task_exec_policy
}

# ----- edge (alb, cloudfront, waf) -----
moved {
  from = random_password.cf_secret
  to   = module.edge.random_password.cf_secret
}
moved {
  from = aws_lb.main
  to   = module.edge.aws_lb.main
}
moved {
  from = aws_lb_target_group.odoo
  to   = module.edge.aws_lb_target_group.odoo
}
moved {
  from = aws_lb_listener.http
  to   = module.edge.aws_lb_listener.http
}
moved {
  from = aws_lb_listener_rule.allow_cloudfront_secret
  to   = module.edge.aws_lb_listener_rule.allow_cloudfront_secret
}
moved {
  from = aws_cloudfront_cache_policy.odoo_assets
  to   = module.edge.aws_cloudfront_cache_policy.odoo_assets
}
moved {
  from = aws_cloudfront_cache_policy.odoo_static
  to   = module.edge.aws_cloudfront_cache_policy.odoo_static
}
moved {
  from = aws_cloudfront_origin_request_policy.odoo_forward_host
  to   = module.edge.aws_cloudfront_origin_request_policy.odoo_forward_host
}
moved {
  from = aws_cloudfront_distribution.odoo
  to   = module.edge.aws_cloudfront_distribution.odoo
}
moved {
  from = aws_wafv2_web_acl.odoo
  to   = module.edge.aws_wafv2_web_acl.odoo
}

# ----- webhooks (sqs, lambda, api gateway) -----
moved {
  from = aws_sqs_queue.webhook
  to   = module.webhooks.aws_sqs_queue.webhook
}
moved {
  from = aws_sqs_queue.webhook_dlq
  to   = module.webhooks.aws_sqs_queue.webhook_dlq
}
moved {
  from = aws_iam_role.webhook_lambda_role
  to   = module.webhooks.aws_iam_role.webhook_lambda_role
}
moved {
  from = aws_iam_role_policy.webhook_lambda_policy
  to   = module.webhooks.aws_iam_role_policy.webhook_lambda_policy
}
moved {
  from = aws_cloudwatch_log_group.webhook_lambda
  to   = module.webhooks.aws_cloudwatch_log_group.webhook_lambda
}
moved {
  from = aws_lambda_function.webhook_authorizer
  to   = module.webhooks.aws_lambda_function.webhook_authorizer
}
moved {
  from = aws_apigatewayv2_api.webhooks
  to   = module.webhooks.aws_apigatewayv2_api.webhooks
}
moved {
  from = aws_apigatewayv2_integration.webhook_lambda
  to   = module.webhooks.aws_apigatewayv2_integration.webhook_lambda
}
moved {
  from = aws_apigatewayv2_route.webhook_post
  to   = module.webhooks.aws_apigatewayv2_route.webhook_post
}
moved {
  from = aws_cloudwatch_log_group.apigw_access
  to   = module.webhooks.aws_cloudwatch_log_group.apigw_access
}
moved {
  from = aws_apigatewayv2_stage.default
  to   = module.webhooks.aws_apigatewayv2_stage.default
}
moved {
  from = aws_lambda_permission.apigw_invoke
  to   = module.webhooks.aws_lambda_permission.apigw_invoke
}

# ----- ecs (cluster, capacity, services) -----
moved {
  from = aws_ecs_cluster.odoo
  to   = module.ecs.aws_ecs_cluster.odoo
}
moved {
  from = aws_service_discovery_private_dns_namespace.odoo
  to   = module.ecs.aws_service_discovery_private_dns_namespace.odoo
}
moved {
  from = random_id.cp_suffix
  to   = module.ecs.random_id.cp_suffix
}
moved {
  from = aws_ecs_cluster_capacity_providers.odoo
  to   = module.ecs.aws_ecs_cluster_capacity_providers.odoo
}
moved {
  from = aws_cloudwatch_log_group.odoo_logs
  to   = module.ecs.aws_cloudwatch_log_group.odoo_logs
}
moved {
  from = aws_cloudwatch_log_group.fastapi_logs
  to   = module.ecs.aws_cloudwatch_log_group.fastapi_logs
}
moved {
  from = aws_launch_template.ecs
  to   = module.ecs.aws_launch_template.ecs
}
moved {
  from = aws_launch_template.pgbouncer
  to   = module.ecs.aws_launch_template.pgbouncer
}
moved {
  from = aws_launch_template.fastapi
  to   = module.ecs.aws_launch_template.fastapi
}
moved {
  from = aws_autoscaling_group.ecs_asg
  to   = module.ecs.aws_autoscaling_group.ecs_asg
}
moved {
  from = aws_autoscaling_group.pgbouncer_asg
  to   = module.ecs.aws_autoscaling_group.pgbouncer_asg
}
moved {
  from = aws_autoscaling_group.fastapi_asg
  to   = module.ecs.aws_autoscaling_group.fastapi_asg
}
moved {
  from = aws_ecs_capacity_provider.odoo
  to   = module.ecs.aws_ecs_capacity_provider.odoo
}
moved {
  from = aws_ecs_capacity_provider.pgbouncer
  to   = module.ecs.aws_ecs_capacity_provider.pgbouncer
}
moved {
  from = aws_ecs_capacity_provider.fastapi
  to   = module.ecs.aws_ecs_capacity_provider.fastapi
}
moved {
  from = aws_ecs_task_definition.odoo
  to   = module.ecs.aws_ecs_task_definition.odoo
}
moved {
  from = aws_ecs_task_definition.pgbouncer
  to   = module.ecs.aws_ecs_task_definition.pgbouncer
}
moved {
  from = aws_ecs_task_definition.fastapi
  to   = module.ecs.aws_ecs_task_definition.fastapi
}
moved {
  from = aws_ecs_service.odoo
  to   = module.ecs.aws_ecs_service.odoo
}
moved {
  from = aws_ecs_service.pgbouncer
  to   = module.ecs.aws_ecs_service.pgbouncer
}
moved {
  from = aws_ecs_service.fastapi
  to   = module.ecs.aws_ecs_service.fastapi
}
moved {
  from = aws_appautoscaling_target.ecs_target
  to   = module.ecs.aws_appautoscaling_target.ecs_target
}
moved {
  from = aws_appautoscaling_target.fastapi_target
  to   = module.ecs.aws_appautoscaling_target.fastapi_target
}
moved {
  from = aws_appautoscaling_policy.ecs_cpu
  to   = module.ecs.aws_appautoscaling_policy.ecs_cpu
}
moved {
  from = aws_appautoscaling_policy.fastapi_cpu
  to   = module.ecs.aws_appautoscaling_policy.fastapi_cpu
}
moved {
  from = aws_iam_role.fastapi_task_role
  to   = module.ecs.aws_iam_role.fastapi_task_role
}
moved {
  from = aws_iam_role_policy.fastapi_sqs_policy
  to   = module.ecs.aws_iam_role_policy.fastapi_sqs_policy
}
moved {
  from = aws_iam_role_policy.fastapi_exec_policy
  to   = module.ecs.aws_iam_role_policy.fastapi_exec_policy
}
