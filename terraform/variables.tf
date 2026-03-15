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

# ── VPC — bring your own or create new ────────────────────────────────────────
#
# Option A — create a new VPC (default):
#   create_vpc = true          (Terraform creates VPC + 2 private subnets)
#   vpc_cidr   = "10.0.0.0/16" (optional, defaults shown)
#
# Option B — use an existing VPC:
#   create_vpc              = false
#   vpc_id                  = "vpc-..."
#   private_subnet_ids      = ["subnet-...", "subnet-..."]
#   private_subnet_cidrs    = ["10.0.1.0/24", "10.0.2.0/24"]
#   private_route_table_ids = ["rtb-...", "rtb-..."]

variable "create_vpc" {
  description = "Set true to create a new VPC and private subnets. Set false to supply existing IDs below."
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  description = "CIDR block for the new VPC (only used when create_vpc = true)."
  type        = string
  default     = "10.0.0.0/16"
}

variable "vpc_id" {
  description = "Existing VPC ID (required when create_vpc = false)."
  type        = string
  default     = null
  nullable    = true
}

variable "private_subnet_ids" {
  description = "Existing private subnet IDs (required when create_vpc = false)."
  type        = list(string)
  default     = null
  nullable    = true
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks of existing private subnets (required when create_vpc = false)."
  type        = list(string)
  default     = null
  nullable    = true
}

variable "private_route_table_ids" {
  description = "Route table IDs for existing private subnets (required when create_vpc = false)."
  type        = list(string)
  default     = null
  nullable    = true
}

# ── SNS topics — bring your own or create new ──────────────────────────────────
#
# Option A — create new topics (default):
#   create_alarm_topic = true
#   create_alert_topic = true
#
# Option B — use existing topics:
#   create_alarm_topic     = false
#   alarm_sns_topic_arn    = "arn:aws:sns:..."
#   create_alert_topic     = false
#   health_alert_sns_topic_arn = "arn:aws:sns:..."

variable "create_alarm_topic" {
  description = "Set true to create a new SNS topic for CloudWatch alarm notifications."
  type        = bool
  default     = true
}

variable "create_alert_topic" {
  description = "Set true to create a new SNS topic for health event alerts."
  type        = bool
  default     = true
}

variable "alarm_sns_topic_arn" {
  description = "Existing SNS topic ARN for CloudWatch alarm notifications (used when create_alarm_topic = false)."
  type        = string
  default     = ""
}

variable "health_alert_sns_topic_arn" {
  description = "Existing SNS topic ARN for health event alerts (used when create_alert_topic = false). Add PagerDuty/Slack subscriptions to this topic."
  type        = string
  default     = ""
}

variable "alerts_enabled" {
  description = "Enable proactive health event alerts via SNS after each collection cycle."
  type        = bool
  default     = true
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
  default     = "rate(5 minutes)"
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
  default     = 300
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

# ── Excel export ───────────────────────────────────────────────────────────────

# ── Alerting tuning ───────────────────────────────────────────────────────────

variable "digest_window_minutes" {
  description = "Minutes to accumulate events before sending the first incident digest alert."
  type        = number
  default     = 15
}

variable "correlation_window_minutes" {
  description = "Minutes window for grouping same-service events into a single incident."
  type        = number
  default     = 60
}

# ── Consumer API access control ───────────────────────────────────────────────

variable "consumer_api_allowed_cidrs" {
  description = "IP CIDR ranges allowed to call the consumer API. Empty list = no IP restriction (not recommended)."
  type        = list(string)
  default     = []
}

variable "excel_export_enabled" {
  description = "Deploy the Excel exporter Lambda and its daily EventBridge schedule."
  type        = bool
  default     = true
}

variable "excel_export_schedule" {
  description = "EventBridge schedule expression for the daily Excel export."
  type        = string
  default     = "rate(1 day)"
}

variable "export_retention_days" {
  description = "Days to keep Excel reports in S3 before expiry."
  type        = number
  default     = 90
}

variable "exporter_timeout_seconds" {
  description = "Exporter Lambda timeout. Increase for large event sets (pandas + Excel write)."
  type        = number
  default     = 300
}

variable "exporter_memory_mb" {
  description = "Exporter Lambda memory. pandas + xlsxwriter need at least 512 MB."
  type        = number
  default     = 1024
}
