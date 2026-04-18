data "archive_file" "tavily_search_zip" {
  type        = "zip"
  source_file = "${path.module}/functions/tavily_search.py"
  output_path = "${path.module}/functions/tavily_search.zip"
}



# 1. Librarian Lambda
resource "aws_lambda_function" "librarian" {
  filename         = data.archive_file.tavily_search_zip.output_path
  function_name    = "bedrock_agent_librarian"
  role             = aws_iam_role.librarian_lambda_role.arn
  handler          = "tavily_search.lambda_handler"
  runtime          = "python3.12"
  timeout          = 90
  source_code_hash = data.archive_file.tavily_search_zip.output_base64sha256

  environment {
    variables = {
      TAVILY_SECRET_ARN = data.aws_secretsmanager_secret.tavily_api_key.arn
    }
  }
}

resource "aws_lambda_permission" "bedrock_invoke_librarian" {
  statement_id  = "AllowBedrockInvokeLibrarian"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.librarian.function_name
  principal     = "bedrock.amazonaws.com"
  source_arn    = aws_bedrockagent_agent.supervisor.agent_arn
}


