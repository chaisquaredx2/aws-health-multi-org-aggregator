# ─────────────────────────────────────────────────────────────────────────────
# Health Proxy API Gateway  (INTERNAL — VPC-only)
#
# Purpose: Allow VPC-bound Lambda to call health.us-east-1.amazonaws.com
# without internet egress. Lambda → execute-api VPC endpoint → this API GW →
# AWS Service integration → health.us-east-1.amazonaws.com.
#
# Auth: AWS_IAM on every method. Only entities with execute-api:Invoke on this
# API's ARN (the collector Lambda execution role) can call it.
#
# Endpoint type: PRIVATE — only reachable via the execute-api VPC endpoint.
#
# Multi-org note: The integration IAM role (health_proxy_apigw) must be, or
# have been registered as, the delegated Health admin for the org(s) you are
# aggregating. For multiple *separate* AWS Organizations each needing their own
# delegated-admin credentials, deploy one copy of this file per org (use a
# module) and expose each via its own VPC endpoint service (PrivateLink).
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # One entry per Health organisational API method we proxy.
  # Each creates: resource → method → integration → method_response → integration_response
  health_proxy_methods = {
    describe_events_for_organization = {
      path_part = "describe-events-for-organization"
      # X-Amz-Target sent to health.us-east-1.amazonaws.com
      target    = "AmazonHealth.DescribeEventsForOrganization"
      # Action used in the integration URI  (arn:aws:apigateway:us-east-1:health:action/<action>)
      action    = "DescribeEventsForOrganization"
    }
    describe_affected_accounts_for_organization = {
      path_part = "describe-affected-accounts-for-organization"
      target    = "AmazonHealth.DescribeAffectedAccountsForOrganization"
      action    = "DescribeAffectedAccountsForOrganization"
    }
    describe_event_details_for_organization = {
      path_part = "describe-event-details-for-organization"
      target    = "AmazonHealth.DescribeEventDetailsForOrganization"
      action    = "DescribeEventDetailsForOrganization"
    }
    describe_affected_entities_for_organization = {
      path_part = "describe-affected-entities-for-organization"
      target    = "AmazonHealth.DescribeAffectedEntitiesForOrganization"
      action    = "DescribeAffectedEntitiesForOrganization"
    }
  }
}

# ── Private REST API ──────────────────────────────────────────────────────────

resource "aws_api_gateway_rest_api" "health_proxy" {
  name        = "${var.project_name}-health-proxy"
  description = "Internal Health API proxy for VPC-bound Lambda (no VPCE for health.amazonaws.com)"

  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = [aws_vpc_endpoint.execute_api.id]
  }

  tags = {
    Name = "${var.project_name}-health-proxy"
  }
}

# Resource policy: allow invocations only from the execute-api VPC endpoint.
# The collector Lambda execution role additionally needs execute-api:Invoke
# in its identity policy (see iam.tf).
resource "aws_api_gateway_rest_api_policy" "health_proxy" {
  rest_api_id = aws_api_gateway_rest_api.health_proxy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyNonVpce"
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.health_proxy.execution_arn}/*"
        Condition = {
          StringNotEquals = {
            "aws:SourceVpce" = aws_vpc_endpoint.execute_api.id
          }
        }
      },
      {
        Sid       = "AllowVpce"
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.health_proxy.execution_arn}/*"
        Condition = {
          StringEquals = {
            "aws:SourceVpce" = aws_vpc_endpoint.execute_api.id
          }
        }
      }
    ]
  })
}

# ── Resources (one path segment per Health method) ────────────────────────────

resource "aws_api_gateway_resource" "health_proxy" {
  for_each = local.health_proxy_methods

  rest_api_id = aws_api_gateway_rest_api.health_proxy.id
  parent_id   = aws_api_gateway_rest_api.health_proxy.root_resource_id
  path_part   = each.value.path_part
}

# ── Methods (POST + AWS_IAM auth) ─────────────────────────────────────────────

resource "aws_api_gateway_method" "health_proxy" {
  for_each = local.health_proxy_methods

  rest_api_id   = aws_api_gateway_rest_api.health_proxy.id
  resource_id   = aws_api_gateway_resource.health_proxy[each.key].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}

# ── Integrations (AWS Service → health.us-east-1.amazonaws.com) ───────────────
#
# VTL request template: "$input.body"
#   The Lambda sends a well-formed Health API JSON body. The template is a
#   passthrough — API GW forwards it as-is to the Health endpoint.
#   The two request_parameters entries override the outbound Content-Type and
#   X-Amz-Target headers to the values the Health JSON-protocol API requires.
#   Static values in API GW parameter mappings must be wrapped in single quotes.
#
# VTL response template: "$input.body"
#   API GW passes the Health API JSON response (including nextToken) back to
#   Lambda unchanged. Lambda's pagination loop reads nextToken and loops.
#
# credentials: the proxy execution role (health_proxy_apigw) must have
#   health:Describe* permissions and must be the delegated Health admin account
#   (or be able to assume one) — see iam.tf and SPEC.md §10.

resource "aws_api_gateway_integration" "health_proxy" {
  for_each = local.health_proxy_methods

  rest_api_id             = aws_api_gateway_rest_api.health_proxy.id
  resource_id             = aws_api_gateway_resource.health_proxy[each.key].id
  http_method             = aws_api_gateway_method.health_proxy[each.key].http_method
  integration_http_method = "POST"
  type                    = "AWS"

  # Action-based URI: routes to the named Health API operation.
  # Format: arn:aws:apigateway:{region}:{service}:action/{Action}
  uri = "arn:aws:apigateway:us-east-1:health:action/${each.value.action}"

  # IAM role API GW assumes to sign requests to health.us-east-1.amazonaws.com
  credentials = aws_iam_role.health_proxy_apigw.arn

  # NEVER: reject requests that do not match a content-type in request_templates
  passthrough_behavior = "NEVER"

  # ── VTL request mapping ──────────────────────────────────────────────────
  # Static header values use single-quoted literals (API GW mapping syntax).
  request_parameters = {
    # Override Content-Type to the JSON-protocol value Health API requires
    "integration.request.header.Content-Type" = "'application/x-amz-json-1.1'"
    # Identify the specific Health operation (JSON protocol dispatch header)
    "integration.request.header.X-Amz-Target" = "'${each.value.target}'"
  }

  # Passthrough: Lambda's body is already a valid Health API request JSON.
  # The $input.body VTL expression emits the raw request body unchanged.
  request_templates = {
    "application/json" = "$input.body"
  }
}

# ── Method responses ──────────────────────────────────────────────────────────

resource "aws_api_gateway_method_response" "health_proxy_200" {
  for_each = local.health_proxy_methods

  rest_api_id = aws_api_gateway_rest_api.health_proxy.id
  resource_id = aws_api_gateway_resource.health_proxy[each.key].id
  http_method = aws_api_gateway_method.health_proxy[each.key].http_method
  status_code = "200"

  response_models = {
    "application/json" = "Empty"
  }
}

resource "aws_api_gateway_method_response" "health_proxy_400" {
  for_each = local.health_proxy_methods

  rest_api_id = aws_api_gateway_rest_api.health_proxy.id
  resource_id = aws_api_gateway_resource.health_proxy[each.key].id
  http_method = aws_api_gateway_method.health_proxy[each.key].http_method
  status_code = "400"

  response_models = {
    "application/json" = "Error"
  }
}

resource "aws_api_gateway_method_response" "health_proxy_500" {
  for_each = local.health_proxy_methods

  rest_api_id = aws_api_gateway_rest_api.health_proxy.id
  resource_id = aws_api_gateway_resource.health_proxy[each.key].id
  http_method = aws_api_gateway_method.health_proxy[each.key].http_method
  status_code = "500"

  response_models = {
    "application/json" = "Error"
  }
}

# ── Integration responses ─────────────────────────────────────────────────────
#
# VTL response template: "$input.body"
#   Passes the Health API response JSON (events[], nextToken, etc.) back to
#   Lambda unchanged. Lambda's pagination loop reads nextToken and loops.
#
# selection_pattern: regex matched against the HTTP status returned by Health.
#   200 → success (default, empty pattern)
#   4\d{2} → client error (bad request, throttled, etc.)
#   5\d{2} → server error

resource "aws_api_gateway_integration_response" "health_proxy_200" {
  for_each = local.health_proxy_methods

  rest_api_id       = aws_api_gateway_rest_api.health_proxy.id
  resource_id       = aws_api_gateway_resource.health_proxy[each.key].id
  http_method       = aws_api_gateway_method.health_proxy[each.key].http_method
  status_code       = aws_api_gateway_method_response.health_proxy_200[each.key].status_code
  selection_pattern = ""  # default (matches 200)

  # ── VTL response mapping ─────────────────────────────────────────────────
  # Pass Health API response body through to Lambda as-is.
  # Lambda's pagination loop reads the nextToken field from this body.
  response_templates = {
    "application/json" = "$input.body"
  }

  depends_on = [aws_api_gateway_integration.health_proxy]
}

resource "aws_api_gateway_integration_response" "health_proxy_400" {
  for_each = local.health_proxy_methods

  rest_api_id       = aws_api_gateway_rest_api.health_proxy.id
  resource_id       = aws_api_gateway_resource.health_proxy[each.key].id
  http_method       = aws_api_gateway_method.health_proxy[each.key].http_method
  status_code       = aws_api_gateway_method_response.health_proxy_400[each.key].status_code
  selection_pattern = "4\\d{2}"

  response_templates = {
    "application/json" = "$input.body"
  }

  depends_on = [aws_api_gateway_integration.health_proxy]
}

resource "aws_api_gateway_integration_response" "health_proxy_500" {
  for_each = local.health_proxy_methods

  rest_api_id       = aws_api_gateway_rest_api.health_proxy.id
  resource_id       = aws_api_gateway_resource.health_proxy[each.key].id
  http_method       = aws_api_gateway_method.health_proxy[each.key].http_method
  status_code       = aws_api_gateway_method_response.health_proxy_500[each.key].status_code
  selection_pattern = "5\\d{2}"

  response_templates = {
    "application/json" = "$input.body"
  }

  depends_on = [aws_api_gateway_integration.health_proxy]
}

# ── Deployment & stage ────────────────────────────────────────────────────────

resource "aws_api_gateway_deployment" "health_proxy" {
  rest_api_id = aws_api_gateway_rest_api.health_proxy.id

  # Trigger redeployment when any integration config changes
  triggers = {
    redeployment = sha1(jsonencode([
      values(aws_api_gateway_resource.health_proxy),
      values(aws_api_gateway_method.health_proxy),
      values(aws_api_gateway_integration.health_proxy),
      values(aws_api_gateway_integration_response.health_proxy_200),
      values(aws_api_gateway_integration_response.health_proxy_400),
      values(aws_api_gateway_integration_response.health_proxy_500),
      aws_api_gateway_rest_api_policy.health_proxy,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration_response.health_proxy_200,
    aws_api_gateway_integration_response.health_proxy_400,
    aws_api_gateway_integration_response.health_proxy_500,
  ]
}

resource "aws_api_gateway_stage" "health_proxy" {
  rest_api_id   = aws_api_gateway_rest_api.health_proxy.id
  deployment_id = aws_api_gateway_deployment.health_proxy.id
  stage_name    = var.environment

  # Access logging to CloudWatch
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.health_proxy_apigw.arn
    format = jsonencode({
      requestId         = "$context.requestId"
      ip                = "$context.identity.sourceIp"
      httpMethod        = "$context.httpMethod"
      resourcePath      = "$context.resourcePath"
      status            = "$context.status"
      responseLength    = "$context.responseLength"
      integrationLatency = "$context.integrationLatency"
    })
  }

  xray_tracing_enabled = true

  tags = {
    Name = "${var.project_name}-health-proxy-${var.environment}"
  }
}

resource "aws_api_gateway_method_settings" "health_proxy" {
  rest_api_id = aws_api_gateway_rest_api.health_proxy.id
  stage_name  = aws_api_gateway_stage.health_proxy.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled    = true
    logging_level      = "INFO"
    data_trace_enabled = false  # do not log request/response bodies (may contain account data)
  }
}

resource "aws_cloudwatch_log_group" "health_proxy_apigw" {
  name              = "/aws/apigateway/${var.project_name}-health-proxy"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}
