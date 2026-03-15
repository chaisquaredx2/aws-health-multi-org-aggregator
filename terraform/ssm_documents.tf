# ─────────────────────────────────────────────────────────────────────────────
# SSM Automation Documents
#
# Deployed automatically by Terraform so the documents appear in the console
# under Systems Manager → Documents → Owned by me.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_ssm_document" "register_org" {
  name            = "HealthAggregator-RegisterOrg"
  document_type   = "Automation"
  document_format = "YAML"
  content         = file("${path.module}/../ssm/HealthAggregator-RegisterOrg.yaml")

  tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "aws_ssm_document" "test_collection" {
  name            = "HealthAggregator-TestCollection"
  document_type   = "Automation"
  document_format = "YAML"
  content         = file("${path.module}/../ssm/HealthAggregator-TestCollection.yaml")

  tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# ── IAM role for SSM Automation execution ─────────────────────────────────────
# Used when running the documents via the console without specifying a custom role.

resource "aws_iam_role" "ssm_automation" {
  name = "${var.project_name}-ssm-automation"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ssm.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
}

resource "aws_iam_role_policy" "ssm_automation" {
  name = "${var.project_name}-ssm-automation"
  role = aws_iam_role.ssm_automation.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # RegisterOrg: read + write the org registry SSM parameter
      {
        Sid    = "SSMOrgRegistry"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/health-aggregator/*"
      },
      # TestCollection: invoke the collector Lambda
      {
        Sid    = "InvokeCollector"
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = aws_lambda_function.collector.arn
      },
      # TestCollection: read Lambda logs
      {
        Sid    = "ReadLambdaLogs"
        Effect = "Allow"
        Action = [
          "logs:FilterLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ]
        Resource = [
          aws_cloudwatch_log_group.collector.arn,
          "${aws_cloudwatch_log_group.collector.arn}:*",
        ]
      },
      # TestCollection: read CloudWatch metrics
      {
        Sid    = "ReadCWMetrics"
        Effect = "Allow"
        Action = "cloudwatch:GetMetricStatistics"
        Resource = "*"
      },
      # Required by SSM Automation to write execution logs
      {
        Sid    = "SSMAutomationExecution"
        Effect = "Allow"
        Action = [
          "ssm:DescribeAutomationExecutions",
          "ssm:GetAutomationExecution",
        ]
        Resource = "*"
      },
    ]
  })
}
