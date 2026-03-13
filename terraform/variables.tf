variable "aws_region" {
  description = "AWS region for all resources. Health API requires us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix for all resource names."
  type        = string
  default     = "health-aggregator"
}

variable "environment" {
  description = "Deployment environment label (prod, staging, dev)."
  type        = string
  default     = "prod"
}

# ── VPC ───────────────────────────────────────────────────────────────────────

variable "vpc_id" {
  description = "VPC ID where both Lambda functions and VPC endpoints are placed."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for Lambda and Interface VPC endpoint ENIs."
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks of private subnets (used in VPC endpoint security group)."
  type        = list(string)
}

variable "private_route_table_ids" {
  description = "Route table IDs for private subnets (used by DynamoDB Gateway endpoint)."
  type        = list(string)
}

# ── Cross-org IAM ─────────────────────────────────────────────────────────────

variable "cross_org_role_name" {
  description = "Name of the IAM role in each org's delegated admin account that the collector assumes. Must be the same name across all orgs."
  type        = string
  default     = "HealthAggregatorReadRole"
}

# ── Collection tuning ─────────────────────────────────────────────────────────

variable "collection_window_days" {
  description = "Sliding window for event collection and API queries (max 7, matches DynamoDB TTL)."
  type        = number
  default     = 7
}

variable "collection_schedule" {
  description = "EventBridge cron expression for the collector Lambda."
  type        = string
  default     = "rate(15 minutes)"
}

variable "max_concurrent_orgs" {
  description = "Max orgs processed in parallel by the collector (ThreadPoolExecutor)."
  type        = number
  default     = 5
}

variable "account_cache_ttl_hours" {
  description = "Hours before account metadata cache entries expire."
  type        = number
  default     = 24
}

# ── Lambda ────────────────────────────────────────────────────────────────────

variable "lambda_runtime" {
  description = "Lambda Python runtime."
  type        = string
  default     = "python3.12"
}

variable "collector_timeout_seconds" {
  description = "Collector Lambda timeout. Increase for large orgs."
  type        = number
  default     = 300  # 5 min
}

variable "api_timeout_seconds" {
  description = "API Lambda timeout."
  type        = number
  default     = 30
}

variable "collector_memory_mb" {
  type    = number
  default = 512
}

variable "api_memory_mb" {
  type    = number
  default = 256
}

# ── Observability ─────────────────────────────────────────────────────────────

variable "log_retention_days" {
  description = "CloudWatch log group retention."
  type        = number
  default     = 90
}

variable "alarm_sns_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarm notifications. Leave empty to skip alarm actions."
  type        = string
  default     = ""
}
