# ─────────────────────────────────────────
# S3 Bucket — FinOps Reports
# ─────────────────────────────────────────
resource "aws_s3_bucket" "finops_reports" {
  bucket = "finops-reports-${data.aws_caller_identity.current.account_id}"

  tags = {
    Project = "finops-agent"
  }
}

resource "aws_s3_bucket_public_access_block" "finops_reports" {
  bucket                  = aws_s3_bucket.finops_reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_caller_identity" "current" {}

# ─────────────────────────────────────────
# IAM Role for Lambda
# ─────────────────────────────────────────
resource "aws_iam_role" "finops_lambda_role" {
  name = "finops-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = {
    Project = "finops-agent"
  }
}

resource "aws_iam_role_policy" "finops_lambda_policy" {
  name = "finops-lambda-policy"
  role = aws_iam_role.finops_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics",
          "ec2:DescribeInstances",
          "ec2:StartInstances",
          "ec2:StopInstances",
          "sns:Publish"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.finops_reports.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "*"
      }
    ]
  })
}

# ─────────────────────────────────────────
# Lambda Deployment Package (zip)
# ─────────────────────────────────────────
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/../lambda/finops_lambda.py"
  output_path = "${path.module}/../lambda/finops_lambda.zip"
}

# ─────────────────────────────────────────
# Lambda Function
# ─────────────────────────────────────────
resource "aws_lambda_function" "finops_agent" {
  function_name    = "finops-agent"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  role             = aws_iam_role.finops_lambda_role.arn
  handler          = "finops_lambda.handler"
  runtime          = "python3.12"
  timeout          = 120

  environment {
    variables = {
      AWS_REGION_NAME = var.aws_region
      SNS_TOPIC_ARN   = aws_sns_topic.finops_alerts.arn
      S3_BUCKET       = aws_s3_bucket.finops_reports.bucket
      ALERT_EMAIL     = var.alert_email
    }
  }

  tags = {
    Project = "finops-agent"
  }
}

# ─────────────────────────────────────────
# EventBridge Rule — Daily 8 AM UTC
# ─────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "daily_finops" {
  name                = "finops-daily-trigger"
  description         = "Trigger FinOps agent every day at 8 AM UTC"
  schedule_expression = "cron(0 8 * * ? *)"

  tags = {
    Project = "finops-agent"
  }
}

resource "aws_cloudwatch_event_target" "invoke_lambda" {
  rule      = aws_cloudwatch_event_rule.daily_finops.name
  target_id = "finops-lambda"
  arn       = aws_lambda_function.finops_agent.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.finops_agent.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_finops.arn
}

# ─────────────────────────────────────────
# SES Email Identity Verification
# ─────────────────────────────────────────
resource "aws_ses_email_identity" "alert_email" {
  email = var.alert_email
}
