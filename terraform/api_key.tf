# ─────────────────────────────────────────────────────────────────────────────
# Consumer API — resource policy (IAM auth + IP allowlist)
#
# Authentication: AWS_IAM (SigV4). Callers must sign requests with valid
# AWS credentials that have execute-api:Invoke permission.
#
# IP restriction: set consumer_api_allowed_cidrs in terraform.tfvars to
# restrict access to specific CIDRs (corporate egress, VPN, etc.).
# ─────────────────────────────────────────────────────────────────────────────

locals {
  _ip_condition = length(var.consumer_api_allowed_cidrs) > 0 ? {
    IpAddress = { "aws:SourceIp" = var.consumer_api_allowed_cidrs }
  } : null
}

resource "aws_api_gateway_rest_api_policy" "consumer" {
  rest_api_id = aws_api_gateway_rest_api.consumer.id

  policy = length(var.consumer_api_allowed_cidrs) > 0 ? jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyNonApprovedIPs"
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.consumer.execution_arn}/*"
        Condition = {
          NotIpAddress = { "aws:SourceIp" = var.consumer_api_allowed_cidrs }
        }
      },
      {
        Sid       = "AllowApprovedIPs"
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.consumer.execution_arn}/*"
      },
    ]
  }) : jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowAll"
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.consumer.execution_arn}/*"
      },
    ]
  })
}

