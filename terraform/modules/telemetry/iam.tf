# IAM for the telemetry tasks. A dedicated execution role (rather than the
# shared one) keeps secret access scoped to the telemetry secrets and avoids a
# cross-module dependency cycle with the iam module.

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- Execution role (image pull, logs, secret injection) ---
resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-telemetry-exec-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "${var.name_prefix}-telemetry-exec-secrets"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.telemetry.arn, var.integration_secret_arn]
      }
    ]
  })
}

# --- ClickHouse task role: read/write the cold-storage bucket ---
resource "aws_iam_role" "clickhouse_task" {
  name               = "${var.name_prefix}-clickhouse-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "clickhouse_s3" {
  name = "${var.name_prefix}-clickhouse-s3"
  role = aws_iam_role.clickhouse_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.clickhouse_cold.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.clickhouse_cold.arn}/*"
      },
    ]
  })
}

# --- Langfuse task role: read/write the events bucket ---
resource "aws_iam_role" "langfuse_task" {
  name               = "${var.name_prefix}-langfuse-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "langfuse_s3" {
  name = "${var.name_prefix}-langfuse-s3"
  role = aws_iam_role.langfuse_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.langfuse_events.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.langfuse_events.arn}/*"
      },
    ]
  })
}

# --- ECS infrastructure role for managing the ClickHouse EBS volume ---
data "aws_iam_policy_document" "ecs_infra_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_ebs_infra" {
  name               = "${var.name_prefix}-ecs-ebs-infra-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_infra_trust.json
}

resource "aws_iam_role_policy_attachment" "ecs_ebs_infra" {
  role       = aws_iam_role.ecs_ebs_infra.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRolePolicyForVolumes"
}
