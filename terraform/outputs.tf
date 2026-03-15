output "consumer_api_url" {
  description = "Base URL of the consumer-facing REST API."
  value       = "https://${aws_api_gateway_rest_api.consumer.id}.execute-api.${var.aws_region}.amazonaws.com/${aws_api_gateway_stage.consumer.stage_name}/v1"
}

output "health_proxy_api_url" {
  description = "Internal Health Proxy API GW URL (VPC-only, used by Lambda env var)."
  value       = "https://${aws_api_gateway_rest_api.health_proxy.id}.execute-api.${var.aws_region}.amazonaws.com/${aws_api_gateway_stage.health_proxy.stage_name}"
  sensitive   = false
}

output "events_table_name" {
  value = aws_dynamodb_table.events.name
}

output "collector_function_name" {
  value = aws_lambda_function.collector.function_name
}

output "execute_api_vpce_id" {
  description = "VPC endpoint ID for execute-api — required by the Health Proxy private API resource policy."
  value       = aws_vpc_endpoint.execute_api.id
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "excel_export_bucket" {
  description = "S3 bucket where daily Excel health reports are stored."
  value       = aws_s3_bucket.exports.id
}

output "excel_export_bucket_arn" {
  value = aws_s3_bucket.exports.arn
}

output "exporter_function_name" {
  description = "Name of the Excel exporter Lambda (invoke manually to generate a report on-demand)."
  value       = var.excel_export_enabled ? aws_lambda_function.exporter[0].function_name : ""
}

output "vpc_id" {
  description = "VPC ID (created or supplied)."
  value       = local.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs (created or supplied)."
  value       = local.private_subnet_ids
}

output "alarm_sns_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarm notifications (created or supplied)."
  value       = local.alarm_sns_topic_arn
}

output "health_alert_sns_topic_arn" {
  description = "SNS topic ARN for health event alerts (created or supplied)."
  value       = local.health_alert_sns_topic_arn
}

output "ssm_automation_role_arn" {
  description = "IAM role ARN to specify when executing SSM Automation documents from the console."
  value       = aws_iam_role.ssm_automation.arn
}
