output "cluster_id" { value = aws_ecs_cluster.odoo.id }
output "cluster_name" { value = aws_ecs_cluster.odoo.name }
output "namespace_arn" { value = aws_service_discovery_private_dns_namespace.odoo.arn }
output "odoo_service_name" { value = aws_ecs_service.odoo.name }
output "fastapi_service_name" { value = aws_ecs_service.fastapi.name }
output "clickhouse_capacity_provider_name" { value = aws_ecs_capacity_provider.clickhouse.name }
