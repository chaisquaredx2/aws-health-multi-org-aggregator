# ─────────────────────────────────────────────────────────────────────────────
# Consumer API — API Key + Usage Plan
#
# The dashboard frontend authenticates with X-Api-Key header.
# The key value is exposed as a sensitive Terraform output.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_api_gateway_api_key" "dashboard" {
  name    = "${var.project_name}-dashboard"
  enabled = true

  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
}

resource "aws_api_gateway_usage_plan" "dashboard" {
  name = "${var.project_name}-dashboard"

  api_stages {
    api_id = aws_api_gateway_rest_api.consumer.id
    stage  = aws_api_gateway_stage.consumer.stage_name
  }

  throttle_settings {
    rate_limit  = 50   # requests per second
    burst_limit = 100
  }

  quota_settings {
    limit  = 10000
    period = "DAY"
  }
}

resource "aws_api_gateway_usage_plan_key" "dashboard" {
  key_id        = aws_api_gateway_api_key.dashboard.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.dashboard.id
}

output "dashboard_api_key" {
  description = "API key for the Health Aggregator dashboard frontend (X-Api-Key header)."
  value       = aws_api_gateway_api_key.dashboard.value
  sensitive   = true
}
