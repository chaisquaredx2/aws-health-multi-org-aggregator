# ─────────────────────────────────────────────────────────────────────────────
# IAM roles and policies
# ─────────────────────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ── 1. Health Proxy API GW execution role ─────────────────────────────────────
# API GW assumes this role to sign and forward requests to health.us-east-1.amazonaws.com.
# This role must be (or be registered as) the delegated Health admin for the org(s).

resource "aws_iam_role" "health_proxy_apigw" {
  name = "${var.project_name}-health-proxy-apigw"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${var.project_name}-health-proxy-apigw" }
}

resource "aws_iam_role_policy" "health_proxy_apigw" {
  name = "health-org-read"
  role = aws_iam_role.health_proxy_apigw.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "HealthOrgRead"
      Effect = "Allow"
      Action = [
        "health:DescribeEventsForOrganization",
        "health:DescribeAffectedAccountsForOrganization",
        "health:DescribeEventDetailsForOrganization",
        "health:DescribeAffectedEntitiesForOrganization",
      ]
      Resource = "*"
      # Health API does not support resource-level restrictions; * is required.
    }]
  })
}

# ── 2. Collector Lambda execution role ────────────────────────────────────────

resource "aws_iam_role" "collector" {
  name = "${var.project_name}-collector"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${var.project_name}-collector" }
}

resource "aws_iam_role_policy_attachment" "collector_basic" {
  role       = aws_iam_role.collector.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "collector" {
  name = "collector-permissions"
  role = aws_iam_role.collector.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Call the Health Proxy API GW
      {
        Sid      = "InvokeHealthProxy"
        Effect   = "Allow"
        Action   = "execute-api:Invoke"
        Resource = "${aws_api_gateway_rest_api.health_proxy.execution_arn}/*"
      },
      # Assume cross-org roles for Organizations API
      {
        Sid      = "AssumeOrgRoles"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        # Scope to roles with the expected naming pattern; tighten in tfvars
        Resource = "arn:aws:iam::*:role/${var.cross_org_role_name}"
      },
      # Write events to DynamoDB
      {
        Sid    = "DynamoDBWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:BatchWriteItem",
        ]
        Resource = [
          aws_dynamodb_table.events.arn,
          aws_dynamodb_table.collection_state.arn,
          aws_dynamodb_table.account_metadata.arn,
        ]
      },
      # Read account metadata cache (batch_get_item for cache hits)
      {
        Sid    = "DynamoDBRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Scan",
        ]
        Resource = aws_dynamodb_table.account_metadata.arn
      },
      # Read org registry from SSM
      {
        Sid      = "SSMRead"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/health-aggregator/*"
      },
      # KMS decrypt for SSM SecureString and DynamoDB
      {
        Sid    = "KMSDecrypt"
        Effect = "Allow"
        Action = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.main.arn
      },
      # Emit custom CloudWatch metrics
      {
        Sid      = "CloudWatchMetrics"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "HealthAggregator" }
        }
      },
      # Publish health event alerts to SNS (delivered from AWS network to PagerDuty/email/Slack)
      {
        Sid      = "SNSPublishAlerts"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = local.health_alert_sns_topic_arn != "" ? local.health_alert_sns_topic_arn : "arn:aws:sns:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ── 3. API Lambda execution role ──────────────────────────────────────────────

resource "aws_iam_role" "api" {
  name = "${var.project_name}-api"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${var.project_name}-api" }
}

resource "aws_iam_role_policy_attachment" "api_basic" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "api" {
  name = "api-permissions"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Read events from DynamoDB (all indexes)
      {
        Sid    = "DynamoDBRead"
        Effect = "Allow"
        Action = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:BatchGetItem"]
        Resource = [
          aws_dynamodb_table.events.arn,
          "${aws_dynamodb_table.events.arn}/index/*",
          aws_dynamodb_table.collection_state.arn,
        ]
      },
      # Read org registry + KMS
      {
        Sid      = "SSMRead"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/health-aggregator/*"
      },
      {
        Sid    = "KMSDecrypt"
        Effect = "Allow"
        Action = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.main.arn
      },
      # API Lambda also calls Health Proxy for live event descriptions (details endpoint)
      {
        Sid      = "InvokeHealthProxy"
        Effect   = "Allow"
        Action   = "execute-api:Invoke"
        Resource = "${aws_api_gateway_rest_api.health_proxy.execution_arn}/*"
      },
    ]
  })
}

# ── 4. API GW CloudWatch logging role (account-level, idempotent) ─────────────

resource "aws_iam_role" "apigw_cloudwatch" {
  name = "${var.project_name}-apigw-cw-logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "apigw_cloudwatch" {
  role       = aws_iam_role.apigw_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "main" {
  cloudwatch_role_arn = aws_iam_role.apigw_cloudwatch.arn
}

# ── 5. Exporter Lambda execution role ─────────────────────────────────────────

resource "aws_iam_role" "exporter" {
  count = var.excel_export_enabled ? 1 : 0
  name  = "${var.project_name}-exporter"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${var.project_name}-exporter" }
}

resource "aws_iam_role_policy_attachment" "exporter_basic" {
  count      = var.excel_export_enabled ? 1 : 0
  role       = aws_iam_role.exporter[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "exporter" {
  count = var.excel_export_enabled ? 1 : 0
  name  = "exporter-permissions"
  role  = aws_iam_role.exporter[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Read all events from DynamoDB for export
      {
        Sid    = "DynamoDBRead"
        Effect = "Allow"
        Action = ["dynamodb:Scan", "dynamodb:Query", "dynamodb:GetItem", "dynamodb:BatchGetItem"]
        Resource = [
          aws_dynamodb_table.events.arn,
          "${aws_dynamodb_table.events.arn}/index/*",
        ]
      },
      # Write Excel reports and state to S3
      {
        Sid    = "S3ExportWrite"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.exports.arn}/*"
      },
      # KMS for DynamoDB and S3 SSE
      {
        Sid    = "KMSDecryptEncrypt"
        Effect = "Allow"
        Action = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.main.arn
      },
    ]
  })
}

# ── Dashboard consumer policy ──────────────────────────────────────────────
# Attach this managed policy to any IAM user/role that needs dashboard access.
# e.g.: aws iam attach-user-policy --user-name bob --policy-arn <output>

resource "aws_iam_policy" "dashboard_consumer" {
  name        = "${var.project_name}-dashboard-consumer"
  description = "Allows calling the Health Aggregator consumer API (execute-api:Invoke with SigV4)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeConsumerAPI"
        Effect   = "Allow"
        Action   = "execute-api:Invoke"
        Resource = "${aws_api_gateway_rest_api.consumer.execution_arn}/*"
      }
    ]
  })
}

output "dashboard_consumer_policy_arn" {
  description = "Attach this policy to any IAM user/role that needs dashboard access."
  value       = aws_iam_policy.dashboard_consumer.arn
}
