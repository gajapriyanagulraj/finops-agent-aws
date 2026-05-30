terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ─────────────────────────────────────────
# SNS Topic + Email Subscription
# ─────────────────────────────────────────
resource "aws_sns_topic" "finops_alerts" {
  name = "finops-alerts"

  tags = {
    Project = "finops-agent"
  }
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.finops_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ─────────────────────────────────────────
# IAM Role for EC2 (CloudWatch + SNS access)
# ─────────────────────────────────────────
resource "aws_iam_role" "finops_ec2_role" {
  name = "finops-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = {
    Project = "finops-agent"
  }
}

resource "aws_iam_role_policy" "finops_policy" {
  name = "finops-policy"
  role = aws_iam_role.finops_ec2_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics",
          "ec2:DescribeInstances",
          "sns:Publish"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "finops_profile" {
  name = "finops-ec2-profile"
  role = aws_iam_role.finops_ec2_role.name
}

# ─────────────────────────────────────────
# Security Group
# ─────────────────────────────────────────
resource "aws_security_group" "finops_sg" {
  name        = "finops-sg"
  description = "FinOps demo EC2 - SSH only"

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "finops-sg"
    Project = "finops-agent"
  }
}

# ─────────────────────────────────────────
# EC2 Instance
# ─────────────────────────────────────────
data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

resource "aws_instance" "finops_demo" {
  ami                    = data.aws_ami.amazon_linux.id
  instance_type          = "t3.micro"
  iam_instance_profile   = aws_iam_instance_profile.finops_profile.name
  vpc_security_group_ids = [aws_security_group.finops_sg.id]

  tags = {
    Name    = "finops-demo"
    Project = "finops-agent"
  }
}

# ─────────────────────────────────────────
# CloudWatch Alarm — Low CPU
# ─────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "low_cpu" {
  alarm_name          = "finops-low-cpu"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Average"
  threshold           = 5
  alarm_description   = "EC2 CPU below 5% — potentially underutilized"
  alarm_actions       = [aws_sns_topic.finops_alerts.arn]

  dimensions = {
    InstanceId = aws_instance.finops_demo.id
  }

  tags = {
    Project = "finops-agent"
  }
}

# ─────────────────────────────────────────
# CloudWatch Dashboard
# ─────────────────────────────────────────
resource "aws_cloudwatch_dashboard" "finops" {
  dashboard_name = "FinOps-Dashboard"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "EC2 CPU Utilization — finops-demo"
          region  = var.aws_region
          period  = 300
          stat    = "Average"
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/EC2", "CPUUtilization", "InstanceId", aws_instance.finops_demo.id]
          ]
          annotations = {
            horizontal = [
              {
                label = "Underutilized threshold"
                value = 5
                color = "#ff6961"
              }
            ]
          }
        }
      },
      {
        type   = "alarm"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "FinOps Alarms"
          alarms = [aws_cloudwatch_metric_alarm.low_cpu.arn]
        }
      }
    ]
  })
}
