resource "aws_kms_key" "main" {
  description             = "${var.project_name} — DynamoDB, SSM, Lambda env encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RootAccess"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "LambdaDecrypt"
        Effect = "Allow"
        Principal = {
          AWS = [
            aws_iam_role.collector.arn,
            aws_iam_role.api.arn,
          ]
        }
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = "*"
      },
      {
        Sid       = "DynamoDB"
        Effect    = "Allow"
        Principal = { Service = "dynamodb.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource  = "*"
      },
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.main.key_id
}
