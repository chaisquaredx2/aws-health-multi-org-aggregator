# ── Lambda packages ───────────────────────────────────────────────────────────

data "archive_file" "exporter" {
  count       = var.excel_export_enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/../lambda/exporter"
  output_path = "${path.module}/../.build/exporter.zip"
}

data "archive_file" "collector" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/collector"
  output_path = "${path.module}/../.build/collector.zip"
}

data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/api"
  output_path = "${path.module}/../.build/api.zip"
}

# ── Shared Lambda security group ──────────────────────────────────────────────

resource "aws_security_group" "lambda" {
  name        = "${var.project_name}-lambda-sg"
  description = "Lambda functions — HTTPS egress to VPC endpoints only"
  vpc_id      = local.vpc_id

  egress {
    description = "HTTPS to VPC endpoint ENIs (execute-api, ssm, sts, logs)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = local.private_subnet_cidrs
  }

  # No ingress — Lambda is invoked by EventBridge / API GW, not by inbound TCP
  tags = { Name = "${var.project_name}-lambda-sg" }
}

# ── Collector Lambda ──────────────────────────────────────────────────────────

resource "aws_lambda_function" "collector" {
  function_name = "${var.project_name}-collector"
  description   = "Collects AWS Health org events via the Health Proxy API GW"
  role          = aws_iam_role.collector.arn
  handler       = "handler.handler"
  runtime       = var.lambda_runtime
  timeout       = var.collector_timeout_seconds
  memory_size   = var.collector_memory_mb
  filename      = data.archive_file.collector.output_path
  source_code_hash = data.archive_file.collector.output_base64sha256

  vpc_config {
    subnet_ids         = local.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      TABLE_NAME                 = aws_dynamodb_table.events.name
      STATE_TABLE_NAME           = aws_dynamodb_table.collection_state.name
      ACCOUNT_METADATA_TABLE_NAME = aws_dynamodb_table.account_metadata.name
      # URL resolved via execute-api VPC endpoint private DNS
      HEALTH_PROXY_API_URL       = "https://${aws_api_gateway_rest_api.health_proxy.id}.execute-api.${var.aws_region}.amazonaws.com/${aws_api_gateway_stage.health_proxy.stage_name}"
      ORG_REGISTRY_PATH          = "/health-aggregator/orgs"
      COLLECTION_WINDOW_DAYS     = tostring(var.collection_window_days)
      MAX_CONCURRENT_ORGS        = tostring(var.max_concurrent_orgs)
      ACCOUNT_CACHE_TTL_HOURS    = tostring(var.account_cache_ttl_hours)
      LOG_LEVEL                  = "INFO"
    }
  }

  kms_key_arn = aws_kms_key.main.arn

  tracing_config { mode = "Active" }

  depends_on = [
    aws_iam_role_policy.collector,
    aws_cloudwatch_log_group.collector,
  ]

  tags = { Name = "${var.project_name}-collector" }
}

resource "aws_cloudwatch_log_group" "collector" {
  name              = "/aws/lambda/${var.project_name}-collector"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# ── API Lambda ────────────────────────────────────────────────────────────────

resource "aws_lambda_function" "api" {
  function_name = "${var.project_name}-api"
  description   = "Serves GET /v1/events, /v1/summary, /v1/orgs"
  role          = aws_iam_role.api.arn
  handler       = "handler.handler"
  runtime       = var.lambda_runtime
  timeout       = var.api_timeout_seconds
  memory_size   = var.api_memory_mb
  filename      = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  vpc_config {
    subnet_ids         = local.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      TABLE_NAME             = aws_dynamodb_table.events.name
      STATE_TABLE_NAME       = aws_dynamodb_table.collection_state.name
      HEALTH_PROXY_API_URL   = "https://${aws_api_gateway_rest_api.health_proxy.id}.execute-api.${var.aws_region}.amazonaws.com/${aws_api_gateway_stage.health_proxy.stage_name}"
      ORG_REGISTRY_PATH      = "/health-aggregator/orgs"
      COLLECTION_WINDOW_DAYS = tostring(var.collection_window_days)
      LOG_LEVEL              = "INFO"
    }
  }

  kms_key_arn = aws_kms_key.main.arn

  tracing_config { mode = "Active" }

  depends_on = [
    aws_iam_role_policy.api,
    aws_cloudwatch_log_group.api,
  ]

  tags = { Name = "${var.project_name}-api" }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.project_name}-api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# ── Consumer API Gateway (external-facing) ────────────────────────────────────
# This is the API that dashboard consumers call (IAM SigV4 or API key).
# Separate from the internal health-proxy API GW.

resource "aws_api_gateway_rest_api" "consumer" {
  name        = "${var.project_name}-api"
  description = "Health aggregator consumer API — events, summary, orgs"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "v1" {
  rest_api_id = aws_api_gateway_rest_api.consumer.id
  parent_id   = aws_api_gateway_rest_api.consumer.root_resource_id
  path_part   = "v1"
}

# Proxy resource: catch all /v1/{proxy+} and route to the API Lambda
resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.consumer.id
  parent_id   = aws_api_gateway_resource.v1.id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "proxy" {
  rest_api_id   = aws_api_gateway_rest_api.consumer.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "proxy" {
  rest_api_id             = aws_api_gateway_rest_api.consumer.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_lambda_permission" "consumer_apigw" {
  statement_id  = "AllowConsumerAPIGW"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.consumer.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "consumer" {
  rest_api_id = aws_api_gateway_rest_api.consumer.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.proxy,
      aws_api_gateway_method.proxy,
      aws_api_gateway_integration.proxy,
    ]))
  }

  lifecycle { create_before_destroy = true }
  depends_on = [aws_api_gateway_integration.proxy]
}

resource "aws_api_gateway_stage" "consumer" {
  rest_api_id   = aws_api_gateway_rest_api.consumer.id
  deployment_id = aws_api_gateway_deployment.consumer.id
  stage_name    = var.environment

  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.consumer_apigw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      integrationLatency = "$context.integrationLatency"
    })
  }
}

resource "aws_cloudwatch_log_group" "consumer_apigw" {
  name              = "/aws/apigateway/${var.project_name}-consumer"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# ── Exporter Lambda (daily Excel report → S3) ─────────────────────────────────

resource "aws_lambda_function" "exporter" {
  count         = var.excel_export_enabled ? 1 : 0
  function_name = "${var.project_name}-exporter"
  description   = "Generates daily Excel health report (pivots, delta) and uploads to S3"
  role          = aws_iam_role.exporter[0].arn
  handler       = "handler.handler"
  runtime       = var.lambda_runtime
  timeout       = var.exporter_timeout_seconds
  memory_size   = var.exporter_memory_mb
  filename      = data.archive_file.exporter[0].output_path
  source_code_hash = data.archive_file.exporter[0].output_base64sha256

  vpc_config {
    subnet_ids         = local.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      TABLE_NAME             = aws_dynamodb_table.events.name
      EXPORT_BUCKET          = aws_s3_bucket.exports.id
      COLLECTION_WINDOW_DAYS = tostring(var.collection_window_days)
      LOG_LEVEL              = "INFO"
    }
  }

  kms_key_arn = aws_kms_key.main.arn

  tracing_config { mode = "Active" }

  depends_on = [
    aws_iam_role_policy.exporter,
    aws_cloudwatch_log_group.exporter,
  ]

  tags = { Name = "${var.project_name}-exporter" }
}

resource "aws_cloudwatch_log_group" "exporter" {
  count             = var.excel_export_enabled ? 1 : 0
  name              = "/aws/lambda/${var.project_name}-exporter"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}
