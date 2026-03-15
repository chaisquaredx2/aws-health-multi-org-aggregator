# ─────────────────────────────────────────────────────────────────────────────
# Optional networking — VPC, subnets, route tables, SNS topics
#
# Set create_vpc = true  to let Terraform build a private VPC from scratch.
# Set create_vpc = false to supply your own vpc_id / subnet / route table IDs.
#
# Set create_alarm_topic = true  to create the CloudWatch alarm SNS topic.
# Set create_alert_topic = true  to create the health-event alert SNS topic.
# Set either to false and supply the ARN in alarm_sns_topic_arn /
# health_alert_sns_topic_arn instead.
# ─────────────────────────────────────────────────────────────────────────────

data "aws_availability_zones" "available" {
  state = "available"
}

# ── VPC ───────────────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  count = var.create_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "${var.project_name}-vpc" }
}

# ── Private subnets (one per AZ, up to 2) ─────────────────────────────────────
# Subnets are /20 slices of the VPC CIDR — 4 096 IPs each.
# No internet gateway or NAT: all AWS service traffic goes via VPC endpoints.

resource "aws_subnet" "private" {
  count = var.create_vpc ? 2 : 0

  vpc_id            = aws_vpc.main[0].id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${var.project_name}-private-${count.index + 1}" }
}

# ── Route table for private subnets ───────────────────────────────────────────
# Gateway endpoint routes (DynamoDB, S3) are injected by vpc_endpoints.tf.
# No 0.0.0.0/0 route — intentional; there is no internet egress.

resource "aws_route_table" "private" {
  count  = var.create_vpc ? 1 : 0
  vpc_id = aws_vpc.main[0].id

  tags = { Name = "${var.project_name}-private-rt" }
}

resource "aws_route_table_association" "private" {
  count = var.create_vpc ? 2 : 0

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

# ── SNS topic: CloudWatch alarm notifications ─────────────────────────────────

resource "aws_sns_topic" "alarm" {
  count = var.create_alarm_topic ? 1 : 0

  name              = "${var.project_name}-alarms"
  kms_master_key_id = aws_kms_key.main.id

  tags = { Name = "${var.project_name}-alarms" }
}

# ── SNS topic: health event alerts (new/changed operational events) ────────────

resource "aws_sns_topic" "alert" {
  count = var.create_alert_topic ? 1 : 0

  name              = "${var.project_name}-health-alerts"
  kms_master_key_id = aws_kms_key.main.id

  tags = { Name = "${var.project_name}-health-alerts" }
}
