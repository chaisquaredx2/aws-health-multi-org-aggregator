# ─────────────────────────────────────────────────────────────────────────────
# CloudWatch alarms and dashboard
# ─────────────────────────────────────────────────────────────────────────────


# ── Collector Lambda ──────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "collector_errors" {
  alarm_name          = "${var.project_name}-collector-errors"
  alarm_description   = "Collector Lambda invocation errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.collector.function_name }
  statistic           = "Sum"
  period              = 300   # 5 min (one collection cycle)
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "collector_duration" {
  alarm_name          = "${var.project_name}-collector-duration-high"
  alarm_description   = "Collector Lambda approaching timeout (>${var.collector_timeout_seconds * 0.8}s)"
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions          = { FunctionName = aws_lambda_function.collector.function_name }
  extended_statistic  = "p95"
  period              = 300   # 5 min (one collection cycle)
  evaluation_periods  = 3
  threshold           = var.collector_timeout_seconds * 1000 * 0.8  # 80% of timeout in ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# Custom metric: per-org collection errors emitted by collector/handler.py
resource "aws_cloudwatch_metric_alarm" "collection_errors_custom" {
  alarm_name          = "${var.project_name}-org-collection-errors"
  alarm_description   = "One or more orgs failed during health event collection"
  namespace           = "HealthAggregator"
  metric_name         = "CollectionErrors"
  dimensions          = { OrgId = "all" }
  statistic           = "Sum"
  period              = 300   # 5 min (one collection cycle)
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "events_collected_zero" {
  alarm_name          = "${var.project_name}-no-events-collected"
  alarm_description   = "Collector ran but collected zero events — possible Health API or IAM issue"
  namespace           = "HealthAggregator"
  metric_name         = "EventsCollected"
  dimensions          = { OrgId = "all" }
  statistic           = "Sum"
  period              = 3600  # 1 hour (4 collection cycles)
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"
  treat_missing_data  = "breaching"  # alarm if collector didn't run at all
  alarm_actions       = local.alarm_actions
}

# ── API Lambda ────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "api_errors" {
  alarm_name          = "${var.project_name}-api-errors"
  alarm_description   = "API Lambda invocation errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_p99_latency" {
  alarm_name          = "${var.project_name}-api-latency-high"
  alarm_description   = "API Lambda p99 latency > 3s"
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name }
  extended_statistic  = "p99"
  period              = 300
  evaluation_periods  = 3
  threshold           = 3000  # ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# ── Health Proxy API GW ───────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "health_proxy_5xx" {
  alarm_name          = "${var.project_name}-health-proxy-5xx"
  alarm_description   = "Health Proxy API GW returning 5xx — Health API may be degraded"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5XXError"
  dimensions = {
    ApiName  = aws_api_gateway_rest_api.health_proxy.name
    Stage    = aws_api_gateway_stage.health_proxy.stage_name
  }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "health_proxy_4xx" {
  alarm_name          = "${var.project_name}-health-proxy-4xx"
  alarm_description   = "Health Proxy API GW returning 4xx — likely IAM permission or request format issue"
  namespace           = "AWS/ApiGateway"
  metric_name         = "4XXError"
  dimensions = {
    ApiName = aws_api_gateway_rest_api.health_proxy.name
    Stage   = aws_api_gateway_stage.health_proxy.stage_name
  }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# ── DynamoDB ──────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "dynamodb_system_errors" {
  alarm_name          = "${var.project_name}-dynamodb-system-errors"
  alarm_description   = "DynamoDB system errors on events table"
  namespace           = "AWS/DynamoDB"
  metric_name         = "SystemErrors"
  dimensions          = { TableName = aws_dynamodb_table.events.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "dynamodb_throttles" {
  alarm_name          = "${var.project_name}-dynamodb-throttled-requests"
  alarm_description   = "DynamoDB throttled requests (table or GSI) — consider switching to provisioned capacity"
  namespace           = "AWS/DynamoDB"
  metric_name         = "ThrottledRequests"
  dimensions          = { TableName = aws_dynamodb_table.events.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# ── CloudWatch Dashboard ──────────────────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.project_name

  dashboard_body = jsonencode({
    widgets = [
      # Row 1: collection health
      {
        type   = "metric"
        x = 0
        y = 0
        width = 8
        height = 6
        properties = {
          title   = "Events Collected per Run"
          view    = "timeSeries"
          region  = var.aws_region
          period  = 900
          metrics = [["HealthAggregator", "EventsCollected", "OrgId", "all"]]
          stat    = "Sum"
        }
      },
      {
        type   = "metric"
        x = 8
        y = 0
        width = 8
        height = 6
        properties = {
          title   = "Collection Errors (per org)"
          view    = "timeSeries"
          region  = var.aws_region
          period  = 900
          metrics = [["HealthAggregator", "CollectionErrors", "OrgId", "all"]]
          stat    = "Sum"
        }
      },
      {
        type   = "metric"
        x = 16
        y = 0
        width = 8
        height = 6
        properties = {
          title   = "Org Collection Duration (p95 ms)"
          view    = "timeSeries"
          region  = var.aws_region
          period  = 900
          metrics = [["HealthAggregator", "OrgCollectionDurationMs", "OrgId", "all"]]
          stat    = "p95"
        }
      },
      # Row 2: Health proxy
      {
        type   = "metric"
        x = 0
        y = 6
        width = 12
        height = 6
        properties = {
          title   = "Health Proxy API GW - 4xx / 5xx"
          view    = "timeSeries"
          region  = var.aws_region
          period  = 300
          metrics = [
            ["AWS/ApiGateway", "4XXError", "ApiName", aws_api_gateway_rest_api.health_proxy.name, "Stage", aws_api_gateway_stage.health_proxy.stage_name, { stat = "Sum", label = "4xx" }],
            ["AWS/ApiGateway", "5XXError", "ApiName", aws_api_gateway_rest_api.health_proxy.name, "Stage", aws_api_gateway_stage.health_proxy.stage_name, { stat = "Sum", label = "5xx" }],
          ]
        }
      },
      {
        type   = "metric"
        x = 12
        y = 6
        width = 12
        height = 6
        properties = {
          title   = "API Lambda - Errors and Duration"
          view    = "timeSeries"
          region  = var.aws_region
          period  = 60
          metrics = [
            ["AWS/Lambda", "Errors",   "FunctionName", aws_lambda_function.api.function_name, { stat = "Sum",  yAxis = "left",  label = "Errors" }],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.api.function_name, { stat = "p99",  yAxis = "right", label = "p99 ms" }],
          ]
        }
      },
      # Row 3: DynamoDB
      {
        type   = "metric"
        x = 0
        y = 12
        width = 12
        height = 6
        properties = {
          title   = "DynamoDB - Consumed Write/Read Capacity"
          view    = "timeSeries"
          region  = var.aws_region
          period  = 300
          metrics = [
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", aws_dynamodb_table.events.name, { stat = "Sum", label = "Write CU" }],
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits",  "TableName", aws_dynamodb_table.events.name, { stat = "Sum", label = "Read CU" }],
          ]
        }
      },
      {
        type   = "metric"
        x = 12
        y = 12
        width = 12
        height = 6
        properties = {
          title   = "DynamoDB - Throttled Requests"
          view    = "timeSeries"
          region  = var.aws_region
          period  = 300
          metrics = [["AWS/DynamoDB", "ThrottledRequests", "TableName", aws_dynamodb_table.events.name, { stat = "Sum" }]]
        }
      },
    ]
  })
}
