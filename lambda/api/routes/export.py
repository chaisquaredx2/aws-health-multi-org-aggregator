"""
routes/export.py — POST /v1/export

Triggers an on-demand Excel export by invoking the exporter Lambda
asynchronously (InvocationType=Event). Returns 202 immediately; the
export completes in the background and lands at:

  s3://<EXPORT_BUCKET>/exports/YYYY/MM/DD/aws-health-events.xlsx

Requires env vars:
  EXPORTER_FUNCTION_NAME  — name of the exporter Lambda (set by Terraform)
  EXPORT_BUCKET           — S3 bucket name (for response hint)
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_EXPORTER_FUNCTION_NAME = os.environ.get("EXPORTER_FUNCTION_NAME", "")
_EXPORT_BUCKET          = os.environ.get("EXPORT_BUCKET", "")

_lambda_client = boto3.client("lambda")


def trigger_export(query: dict, _multi_query: dict, _path_param=None) -> dict:
    if not _EXPORTER_FUNCTION_NAME:
        return _response(501, {
            "error": {
                "code":    "NOT_CONFIGURED",
                "message": "Excel export is not enabled on this deployment (excel_export_enabled = false)",
            }
        })

    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    s3_key = f"exports/{today}/aws-health-events.xlsx"

    try:
        _lambda_client.invoke(
            FunctionName   = _EXPORTER_FUNCTION_NAME,
            InvocationType = "Event",   # async — fire-and-forget
            Payload        = b"{}",
        )
    except ClientError as exc:
        logger.error("Failed to invoke exporter Lambda: %s", exc)
        return _response(500, {
            "error": {
                "code":    "INTERNAL_ERROR",
                "message": "Failed to start export. Check CloudWatch logs.",
            }
        })

    logger.info("On-demand export triggered: function=%s", _EXPORTER_FUNCTION_NAME)

    body: dict = {"message": "Export started", "estimated_s3_key": s3_key}
    if _EXPORT_BUCKET:
        body["s3_uri"] = f"s3://{_EXPORT_BUCKET}/{s3_key}"

    return _response(202, body)


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type":                 "application/json",
            "Access-Control-Allow-Origin":  "*",
        },
        "body": json.dumps(body),
    }
