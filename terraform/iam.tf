# ECS Task Execution Role
data "aws_iam_policy_document" "ecs_task_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# IAM role for ECS task execution - allows ECS to pull images from ECR and write CloudWatch logs
resource "aws_iam_role" "ecs_task_execution_role" {
  name               = "odoo-ecs-task-execution-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_trust.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_role_policy" {
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# EC2 Instance Role for ECS
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_instance_role" {
  name               = "odoo-ecs-instance-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "ecs_instance_role_policy" {
  role       = aws_iam_role.ecs_instance_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "ecs_instance_profile" {
  name = "odoo-ecs-instance-profile"
  role = aws_iam_role.ecs_instance_role.name
}

# Allow ECS to read the passwords from Secrets Manager and RDS
resource "aws_iam_role_policy" "ecs_secrets_policy" {
  name = "odoo-ecs-secrets-policy"
  role = aws_iam_role.ecs_task_execution_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = ["secretsmanager:GetSecretValue"]
        Effect = "Allow"
        Resource = [
          aws_db_instance.postgres.master_user_secret[0].secret_arn,
          data.aws_secretsmanager_secret.odoo_admin_passwd.arn
        ]
      }
    ]
  })
}

# ECS Task Role - permissions for the containers themselves
resource "aws_iam_role" "ecs_task_role" {
  name               = "odoo-ecs-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_trust.json
}

# Allow the task to mount and write to EFS
resource "aws_iam_role_policy" "ecs_efs_policy" {
  name = "odoo-ecs-efs-policy"
  role = aws_iam_role.ecs_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:ClientRootAccess"
        ]
        Effect   = "Allow"
        Resource = aws_efs_file_system.odoo.arn
      }
    ]
  })
}

# Allow ECS Exec (shell access)
resource "aws_iam_role_policy" "ecs_task_exec_policy" {
  name = "odoo-ecs-task-exec-policy"
  role = aws_iam_role.ecs_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel"
        ]
        Effect   = "Allow"
        Resource = "*"
      }
    ]
  })
}

# --- Bedrock and Multi-Agent IAM ---

# 1. ECS Task capability to Invoke Bedrock Agent
resource "aws_iam_role_policy" "ecs_bedrock_invoke_policy" {
  name = "odoo-ecs-bedrock-invoke"
  role = aws_iam_role.ecs_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "bedrock:InvokeAgent",
          "bedrock:GetAgent",
          "bedrock:GetAgentAlias"
        ]
        Effect = "Allow"
        Resource = [
          aws_bedrockagent_agent.supervisor.agent_arn,
          "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:agent-alias/${aws_bedrockagent_agent.supervisor.id}/*"
        ]
      }
    ]
  })
}


# 2. Bedrock Supervisor Agent Role
resource "aws_iam_role" "bedrock_agent_role" {
  name = "bedrock-supervisor-agent-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Principal = { Service = "bedrock.amazonaws.com" }
        Effect    = "Allow"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:*"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "bedrock_agent_policy" {
  name = "bedrock-supervisor-policy"
  role = aws_iam_role.bedrock_agent_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # 1. Model Invocation & Metadata (Cross-Region supported via Resource *)
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:GetInferenceProfile",
          "bedrock:ListInferenceProfiles",
          "bedrock:GetFoundationModel",
          "bedrock:ListFoundationModels"
        ]
        Effect   = "Allow"
        Resource = ["*"]
      },
      {
        # 2. Agent Identity (Required for orchestration state/alias resolution)
        Action = [
          "bedrock:GetAgent",
          "bedrock:ListAgents",
          "bedrock:GetAgentAlias",
          "bedrock:ListAgentAliases",
          "bedrock:GetAgentKnowledgeBase",
          "bedrock:ListAgentKnowledgeBases"
        ]
        Effect   = "Allow"
        Resource = ["arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:agent*"]
      },
      {
        # 3. Knowledge Base Retrieval
        Action   = ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"]
        Effect   = "Allow"
        Resource = [aws_bedrockagent_knowledge_base.research_kb.arn]
      },
      {
        # 4. Action Group Lambda Invocation
        Action   = ["lambda:InvokeFunction"]
        Effect   = "Allow"
        Resource = [aws_lambda_function.librarian.arn]
      },
      {
        # 5. Native Logging (Trace Delivery)
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Effect = "Allow"
        Resource = [
          aws_cloudwatch_log_group.bedrock_agent_logs.arn,
          "${aws_cloudwatch_log_group.bedrock_agent_logs.arn}:*"
        ]
      }
    ]
  })
}

# 2.1 Allow Bedrock to deliver vended logs to CloudWatch
resource "aws_cloudwatch_log_resource_policy" "bedrock_logging" {
  policy_name = "BedrockLoggingPolicy"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.bedrock_agent_logs.arn,
          "${aws_cloudwatch_log_group.bedrock_agent_logs.arn}:*"
        ]
      }
    ]
  })
}

# 3. Bedrock Knowledge Base Role
resource "aws_iam_role" "bedrock_kb_role" {
  name = "bedrock-knowledge-base-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Principal = { Service = "bedrock.amazonaws.com" }
        Effect    = "Allow"
      }
    ]
  })
}

resource "aws_iam_role_policy" "bedrock_kb_oss_access_policy" {
  name = "bedrock-kb-oss-access"
  role = aws_iam_role.bedrock_kb_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["aoss:APIAccessAll", "aoss:DashboardsAccessAll"]
        Effect   = "Allow"
        Resource = [aws_opensearchserverless_collection.bedrock_kb.arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "bedrock_kb_policy" {
  name = "bedrock-kb-s3-policy"
  role = aws_iam_role.bedrock_kb_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Effect   = "Allow"
        Resource = [aws_s3_bucket.company_research_vault.arn, "${aws_s3_bucket.company_research_vault.arn}/*"]
      },
      {
        Action   = ["bedrock:InvokeModel"]
        Effect   = "Allow"
        Resource = ["arn:aws:bedrock:${data.aws_region.current.id}::foundation-model/amazon.titan-embed-text-v2:0"]
      }
    ]
  })
}

# 4. Lambda Roles
resource "aws_iam_role" "librarian_lambda_role" {
  name = "librarian-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Principal = { Service = "lambda.amazonaws.com" }
        Effect    = "Allow"
      }
    ]
  })
}
resource "aws_iam_role_policy_attachment" "librarian_basic" {
  role       = aws_iam_role.librarian_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "librarian_secrets_policy" {
  name = "librarian-secrets-policy"
  role = aws_iam_role.librarian_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["secretsmanager:GetSecretValue"]
        Effect   = "Allow"
        Resource = data.aws_secretsmanager_secret.tavily_api_key.arn
      }
    ]
  })
}


