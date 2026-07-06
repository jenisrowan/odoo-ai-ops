# Proactive alerting so failures during live hosting page you instead of sitting
# silently in CloudWatch. All alarms notify a single SNS topic; add an email (or
# subscribe Slack/PagerDuty later) to actually receive them.

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

# Optional email subscription (only when an address is provided).
resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# --- Webhook pipeline ------------------------------------------------------

# Anything in the DLQ means webhook processing failed repeatedly — high signal.
resource "aws_cloudwatch_metric_alarm" "webhook_dlq_not_empty" {
  alarm_name          = "${var.name_prefix}-webhook-dlq-not-empty"
  alarm_description   = "Messages landed in the webhook dead-letter queue (processing is failing)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.webhook_dlq_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# The verify+ingest Lambda erroring = webhooks are being dropped at the door.
resource "aws_cloudwatch_metric_alarm" "webhook_lambda_errors" {
  alarm_name          = "${var.name_prefix}-webhook-lambda-errors"
  alarm_description   = "The webhook verify+ingest Lambda is throwing errors."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = var.webhook_lambda_function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# Oldest un-processed webhook getting old = the FastAPI SQS worker is stalled.
resource "aws_cloudwatch_metric_alarm" "webhook_queue_stalled" {
  alarm_name          = "${var.name_prefix}-webhook-queue-stalled"
  alarm_description   = "Webhooks are piling up unprocessed (agent SQS worker stalled?)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = var.webhook_queue_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.queue_age_threshold_seconds
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --- Core services down ----------------------------------------------------

# RunningTaskCount drops to 0 => the service is fully down (via Container Insights).
resource "aws_cloudwatch_metric_alarm" "service_down" {
  for_each = toset(var.monitored_services)

  alarm_name          = "${var.name_prefix}-${each.value}-down"
  alarm_description   = "ECS service ${each.value} has no running tasks."
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = var.cluster_name, ServiceName = each.value }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

output "sns_topic_arn" { value = aws_sns_topic.alerts.arn }
