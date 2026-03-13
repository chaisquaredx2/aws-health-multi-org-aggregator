# ── Events table ──────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "events" {
  name         = "${var.project_name}-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute { name = "pk";               type = "S" }
  attribute { name = "sk";               type = "S" }
  attribute { name = "category";         type = "S" }
  attribute { name = "start_time";       type = "S" }
  attribute { name = "org_id";           type = "S" }
  attribute { name = "last_updated_time"; type = "S" }

  # Primary query path: all events in category within 7-day window
  global_secondary_index {
    name            = "category-starttime-index"
    hash_key        = "category"
    range_key       = "start_time"
    projection_type = "ALL"
  }

  # Per-org event listing + collection state count
  global_secondary_index {
    name            = "org-lastupdate-index"
    hash_key        = "org_id"
    range_key       = "last_updated_time"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "event_arn", "category", "service", "region", "status", "affected_account_count"
    ]
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery { enabled = true }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }

  tags = { Name = "${var.project_name}-events" }
}

# ── Account metadata cache ────────────────────────────────────────────────────

resource "aws_dynamodb_table" "account_metadata" {
  name         = "${var.project_name}-account-metadata"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"  # "{org_id}#{account_id}"

  attribute { name = "pk"; type = "S" }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }

  tags = { Name = "${var.project_name}-account-metadata" }
}

# ── Collection state ──────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "collection_state" {
  name         = "${var.project_name}-collection-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"  # org_id

  attribute { name = "pk"; type = "S" }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }

  tags = { Name = "${var.project_name}-collection-state" }
}
