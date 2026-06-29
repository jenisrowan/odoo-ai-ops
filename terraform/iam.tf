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




