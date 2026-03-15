# ─────────────────────────────────────────────────────────────────────────────
# VPC Endpoints
#
# Required because both Lambdas are VPC-attached and have no internet egress.
# AWS Health has no VPC endpoint — that gap is covered by api_gateway_health_proxy.tf.
#
# execute-api  Interface  Lambda → Health Proxy API GW (private API)
# dynamodb     Gateway    Lambda → DynamoDB (free, no hourly cost)
# ssm          Interface  Lambda → SSM Parameter Store
# sts          Interface  Lambda → STS AssumeRole (for org roles)
# ─────────────────────────────────────────────────────────────────────────────

# ── execute-api (Interface) ───────────────────────────────────────────────────
# Required for Lambda to reach the private Health Proxy API GW and the
# private consumer API GW (if the API Lambda is also VPC-attached).

resource "aws_vpc_endpoint" "execute_api" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.execute-api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true  # lets Lambda use the standard execute-api hostname

  tags = { Name = "${var.project_name}-execute-api-vpce" }
}

# ── DynamoDB (Gateway — free) ─────────────────────────────────────────────────

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.private_route_table_ids

  tags = { Name = "${var.project_name}-dynamodb-vpce" }
}

# ── SSM (Interface) ───────────────────────────────────────────────────────────

resource "aws_vpc_endpoint" "ssm" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.ssm"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-ssm-vpce" }
}

# ── STS (Interface) ───────────────────────────────────────────────────────────
# Collector Lambda assumes cross-org roles via STS.
# Note: STS is global but has regional endpoints; using the regional endpoint
# avoids cross-region calls and respects VPC routing.

resource "aws_vpc_endpoint" "sts" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.sts"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-sts-vpce" }
}

# ── CloudWatch Logs (Interface) ───────────────────────────────────────────────
# Lambda needs to write logs. Without this, log writes fail in a no-egress VPC.

resource "aws_vpc_endpoint" "logs" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-logs-vpce" }
}

# ── SNS (Interface) ───────────────────────────────────────────────────────────
# Collector Lambda publishes health event alerts to SNS. SNS then delivers
# from AWS-managed network to subscribers (PagerDuty HTTPS, email, Slack, etc.)
# without requiring Lambda to reach the internet directly.

resource "aws_vpc_endpoint" "sns" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.sns"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-sns-vpce" }
}

# ── S3 (Gateway — free) ───────────────────────────────────────────────────────
# Exporter Lambda writes Excel reports to S3. Gateway endpoint is free and
# routes S3 traffic through the AWS backbone (no internet egress needed).

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.private_route_table_ids

  tags = { Name = "${var.project_name}-s3-vpce" }
}

# ── Security group shared by all Interface endpoints ──────────────────────────

resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.project_name}-vpce-sg"
  description = "Allow Lambda subnets to reach VPC Interface endpoints on HTTPS"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from Lambda subnets"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
  }

  egress {
    description = "Allow all outbound (endpoint ENIs need to respond)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-vpce-sg" }
}
