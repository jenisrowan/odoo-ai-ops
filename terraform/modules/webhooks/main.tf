# Webhook ingestion: API Gateway -> Lambda (HMAC verify + Slack challenge) -> SQS.
# The FastAPI agent polls the queue privately via the SQS interface VPC endpoint.

# --- SQS ---
resource "aws_sqs_queue" "webhook_dlq" {
  name                      = "${var.name_prefix}-webhook-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "webhook" {
  name                       = "${var.name_prefix}-webhook"
  sqs_managed_sse_enabled    = true
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20     # long polling
  visibility_timeout_seconds = 120

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.webhook_dlq.arn
    maxReceiveCount     = 5
  })
}
# Access is granted via identity-based policies on the Lambda role (below) and
# the FastAPI task role (ecs module), so no SQS resource policy is required.

# --- Lambda (authorizer & ingest) ---
data "archive_file" "webhook_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../../../lambda/webhook_authorizer"
  output_path = "${path.root}/build/webhook_authorizer.zip"
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "webhook_lambda_role" {
  name               = "${var.name_prefix}-webhook-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "webhook_lambda_policy" {
  name = "${var.name_prefix}-webhook-lambda-policy"
  role = aws_iam_role.webhook_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.region}:${var.account_id}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.webhook.arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.integration_secret_arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "webhook_lambda" {
  name              = "/aws/lambda/${var.name_prefix}-webhook-authorizer"
  retention_in_days = 14
}

resource "aws_lambda_function" "webhook_authorizer" {
  function_name    = "${var.name_prefix}-webhook-authorizer"
  role             = aws_iam_role.webhook_lambda_role.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.14"
  architectures    = ["arm64"]
  filename         = data.archive_file.webhook_lambda.output_path
  source_code_hash = data.archive_file.webhook_lambda.output_base64sha256
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      SQS_QUEUE_URL          = aws_sqs_queue.webhook.url
      INTEGRATION_SECRET_ARN = var.integration_secret_arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.webhook_lambda]
}

# --- API Gateway (HTTP API) ---
resource "aws_apigatewayv2_api" "webhooks" {
  name          = "${var.name_prefix}-webhooks"
  protocol_type = "HTTP"
  description   = "Webhook ingress for Shopify and Slack."
}

resource "aws_apigatewayv2_integration" "webhook_lambda" {
  api_id                 = aws_apigatewayv2_api.webhooks.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.webhook_authorizer.invoke_arn
  payload_format_version = "2.0"
  integration_method     = "POST"
}

resource "aws_apigatewayv2_route" "webhook_post" {
  api_id    = aws_apigatewayv2_api.webhooks.id
  route_key = "POST /webhooks/{source}"
  target    = "integrations/${aws_apigatewayv2_integration.webhook_lambda.id}"
}

resource "aws_cloudwatch_log_group" "apigw_access" {
  name              = "/aws/apigateway/${var.name_prefix}-webhooks"
  retention_in_days = 14
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.webhooks.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw_access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      integrationErr = "$context.integrationErrorMessage"
      responseLength = "$context.responseLength"
    })
  }

  default_route_settings {
    throttling_burst_limit = 200
    throttling_rate_limit  = 100
  }
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.webhook_authorizer.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhooks.execution_arn}/*/*"
}

output "queue_url" { value = aws_sqs_queue.webhook.url }
output "queue_arn" { value = aws_sqs_queue.webhook.arn }
output "api_gateway_invoke_url" { value = aws_apigatewayv2_stage.default.invoke_url }

# Host (no scheme) of the API endpoint - used as the CloudFront webhook origin.
output "api_host" {
  value = replace(aws_apigatewayv2_api.webhooks.api_endpoint, "https://", "")
}

# Exposed for terraform test assertions.
output "lambda_runtime" { value = aws_lambda_function.webhook_authorizer.runtime }
output "dlq_arn" { value = aws_sqs_queue.webhook_dlq.arn }

# Exposed for CloudWatch alarm dimensions (observability module).
output "queue_name" { value = aws_sqs_queue.webhook.name }
output "dlq_name" { value = aws_sqs_queue.webhook_dlq.name }
output "lambda_function_name" { value = aws_lambda_function.webhook_authorizer.function_name }
