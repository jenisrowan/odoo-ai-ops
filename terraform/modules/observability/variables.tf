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
