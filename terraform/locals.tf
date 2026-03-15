# ─────────────────────────────────────────────────────────────────────────────
# Locals — resolve "create or bring-your-own" for VPC and SNS topics.
#
# Every other .tf file uses local.vpc_id, local.private_subnet_ids, etc.
# instead of var.* directly. This is the only place that knows about the
# create_vpc / create_*_topic flags.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # ── VPC ──────────────────────────────────────────────────────────────────────
  vpc_id = var.create_vpc ? aws_vpc.main[0].id : var.vpc_id

  private_subnet_ids = var.create_vpc ? aws_subnet.private[*].id : var.private_subnet_ids

  private_subnet_cidrs = var.create_vpc ? [
    for s in aws_subnet.private : s.cidr_block
  ] : var.private_subnet_cidrs

  private_route_table_ids = var.create_vpc ? [
    aws_route_table.private[0].id
  ] : var.private_route_table_ids

  # ── SNS topics ────────────────────────────────────────────────────────────────
  alarm_sns_topic_arn        = var.create_alarm_topic ? aws_sns_topic.alarm[0].arn : var.alarm_sns_topic_arn
  health_alert_sns_topic_arn = var.create_alert_topic ? aws_sns_topic.alert[0].arn : var.health_alert_sns_topic_arn

  # Convenience: alarm_actions list for CloudWatch alarms (empty list = no action)
  alarm_actions = local.alarm_sns_topic_arn != "" ? [local.alarm_sns_topic_arn] : []
}
