output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.finops_demo.id
}

output "instance_public_ip" {
  description = "EC2 public IP address"
  value       = aws_instance.finops_demo.public_ip
}

output "sns_topic_arn" {
  description = "SNS topic ARN for FinOps alerts"
  value       = aws_sns_topic.finops_alerts.arn
}

output "cloudwatch_dashboard_url" {
  description = "CloudWatch dashboard URL"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=FinOps-Dashboard"
}

output "lambda_function_name" {
  description = "FinOps Lambda function name"
  value       = aws_lambda_function.finops_agent.function_name
}

output "s3_reports_bucket" {
  description = "S3 bucket where reports are stored"
  value       = aws_s3_bucket.finops_reports.bucket
}

output "eventbridge_rule" {
  description = "EventBridge schedule rule name"
  value       = aws_cloudwatch_event_rule.daily_finops.name
}
