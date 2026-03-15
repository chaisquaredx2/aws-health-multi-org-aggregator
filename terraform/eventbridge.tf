resource "aws_cloudwatch_event_rule" "collector" {
  name                = "${var.project_name}-collector-schedule"
  description         = "Trigger health event collector every 15 minutes"
  schedule_expression = var.collection_schedule
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "collector" {
  rule      = aws_cloudwatch_event_rule.collector.name
  target_id = "CollectorLambda"
  arn       = aws_lambda_function.collector.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.collector.arn
}

# ── Exporter — daily Excel report ─────────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "exporter" {
  count               = var.excel_export_enabled ? 1 : 0
  name                = "${var.project_name}-exporter-schedule"
  description         = "Trigger Excel exporter daily"
  schedule_expression = var.excel_export_schedule
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "exporter" {
  count     = var.excel_export_enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.exporter[0].name
  target_id = "ExporterLambda"
  arn       = aws_lambda_function.exporter[0].arn
}

resource "aws_lambda_permission" "eventbridge_exporter" {
  count         = var.excel_export_enabled ? 1 : 0
  statement_id  = "AllowEventBridgeInvokeExporter"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.exporter[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.exporter[0].arn
}
