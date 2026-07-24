variable "name_prefix" { type = string }

variable "alarm_email" {
  description = "Email to receive alarm notifications. Empty = create the topic/alarms but no email subscription (subscribe later, or view state in the console)."
  type        = string
  default     = ""
}

# ECS
variable "cluster_name" { type = string }
variable "monitored_services" {
  description = "ECS service names to alarm on when running task count drops to 0."
  type        = list(string)
}

# Webhooks
variable "webhook_queue_name" { type = string }
variable "webhook_dlq_name" { type = string }
variable "webhook_lambda_function_name" { type = string }

variable "queue_age_threshold_seconds" {
  description = "Alarm when the oldest un-processed webhook is older than this (agent stalled)."
  type        = number
  default     = 900
}

# Application errors in logs
variable "error_log_groups" {
  description = <<-EOT
    Log groups to watch for application errors, as {label = log_group_name}. The
    label becomes part of the metric and alarm name, so keep it short and stable.
    Add more groups here to extend the coverage; nothing else needs changing.
  EOT
  type        = map(string)
  default     = {}
}

variable "error_log_pattern" {
  description = <<-EOT
    CloudWatch Logs filter pattern marking a log line as an application error.
    `?` terms are OR-ed. The default catches Python's `Traceback` header and the
    ERROR/CRITICAL level tokens that both Odoo and the agent emit.
    Tighten it here if a noisy third-party line starts crying wolf.
  EOT
  type        = string
  default     = "?ERROR ?CRITICAL ?Traceback"
}

variable "error_alarm_threshold" {
  description = <<-EOT
    Errors within a 5-minute window before alarming. 0 means "tell me about any
    error at all", which is the right default at this volume - raise it if a
    known-benign error turns the alert into noise you learn to ignore.
  EOT
  type        = number
  default     = 0
}
