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

# --- Application errors ----------------------------------------------------
#
# The alarms above answer "is it running?". This one answers "is it working?",
# which is a different question and the one that actually bites: a task can be
# healthy, passing its health check and reporting RunningTaskCount = 1 while
# failing every single request - a wrong database name, a rejected API key, an
# expired token. None of the infrastructure metrics move at all in that state.
#
# What does move is the log: those failures surface as ERROR lines and Python
# tracebacks. So we count them and alarm on the count.
#
# The metric is emitted with default_value = 0 so it reports zero while things
# are healthy rather than going INSUFFICIENT_DATA between errors - otherwise the
# alarm spends its life in a state that looks broken and gets ignored.
locals {
  error_metric_namespace = "${var.name_prefix}/Application"
}

resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each = var.error_log_groups

  name           = "${var.name_prefix}-${each.key}-errors"
  log_group_name = each.value
  pattern        = var.error_log_pattern

  metric_transformation {
    name          = "${each.key}ErrorCount"
    namespace     = local.error_metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  for_each = var.error_log_groups

  alarm_name        = "${var.name_prefix}-${each.key}-errors"
  alarm_description = "Application errors in ${each.value} - the service is up but failing. Check the log group; Logs Insights across all groups will show the request that failed."

  namespace   = local.error_metric_namespace
  metric_name = aws_cloudwatch_log_metric_filter.errors[each.key].metric_transformation[0].name
  statistic   = "Sum"

  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.error_alarm_threshold
  # No logs at all means no errors, not a breach - a quiet night is not an
  # outage. Genuine "the service is gone" is covered by the service_down alarm.
  treat_missing_data = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

output "sns_topic_arn" { value = aws_sns_topic.alerts.arn }
output "error_alarm_names" { value = [for a in aws_cloudwatch_metric_alarm.errors : a.alarm_name] }
