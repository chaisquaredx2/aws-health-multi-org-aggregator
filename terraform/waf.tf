# ─────────────────────────────────────────────────────────────────────────────
# WAF v2 — consumer API Gateway only
#
# The health-proxy API GW is PRIVATE (execute-api VPC endpoint, resource policy
# restricts to VPC endpoint). WAF is not applicable to private APIs.
# WAF is applied only to the consumer-facing REGIONAL API GW.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_wafv2_web_acl" "consumer_api" {
  name  = "${var.project_name}-consumer-api-waf"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  # Rule 1: rate limit per source IP
  rule {
    name     = "RateLimit"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 1000   # requests per 5-minute window per IP
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  # Rule 2: AWS managed common rule set (SQLi, XSS, bad inputs)
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-common-rules"
      sampled_requests_enabled   = false
    }
  }

  # Rule 3: known bad inputs (log4j, etc.)
  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-bad-inputs"
      sampled_requests_enabled   = false
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-waf"
    sampled_requests_enabled   = true
  }

  tags = { Name = "${var.project_name}-consumer-api-waf" }
}

resource "aws_wafv2_web_acl_association" "consumer_api" {
  resource_arn = aws_api_gateway_stage.consumer.arn
  web_acl_arn  = aws_wafv2_web_acl.consumer_api.arn
}

resource "aws_cloudwatch_log_group" "waf" {
  # WAF log group name must start with aws-waf-logs-
  name              = "aws-waf-logs-${var.project_name}-consumer-api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_wafv2_web_acl_logging_configuration" "consumer_api" {
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]
  resource_arn            = aws_wafv2_web_acl.consumer_api.arn

  # Only log blocked requests to avoid high volume on allowed traffic
  logging_filter {
    default_behavior = "DROP"
    filter {
      behavior = "KEEP"
      condition {
        action_condition { action = "BLOCK" }
      }
      requirement = "MEETS_ANY"
    }
  }
}
