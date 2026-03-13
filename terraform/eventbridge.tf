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
