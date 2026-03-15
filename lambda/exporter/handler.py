"""
exporter/handler.py — Excel Export Lambda

Triggered daily by EventBridge. Reads all events from the DynamoDB events
table, generates an Excel workbook with pivots and delta tracking, and
uploads it to S3.

S3 key pattern:
  exports/YYYY/MM/DD/aws-health-events.xlsx      (main report)
  exports/state/open_arns.json                   (state for delta computation)
  exports/delta-log/delta_log.json               (rolling delta history)

Env vars:
  TABLE_NAME         — DynamoDB events table
  EXPORT_BUCKET      — S3 bucket for Excel exports
  COLLECTION_WINDOW_DAYS — window to include in report (default 7)
  LOG_LEVEL
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr

from excel_writer import write_excel, current_open_arns

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_TABLE_NAME    = os.environ["TABLE_NAME"]
_EXPORT_BUCKET = os.environ["EXPORT_BUCKET"]
_WINDOW_DAYS   = int(os.environ.get("COLLECTION_WINDOW_DAYS", "7"))

_dynamodb = boto3.resource("dynamodb")
_table    = _dynamodb.Table(_TABLE_NAME)
_s3       = boto3.client("s3")

_STATE_KEY    = "exports/state/open_arns.json"
_DELTA_LOG_KEY = "exports/delta-log/delta_log.json"


def handler(event: dict, context) -> dict:
    logger.info("Starting Excel export (window=%d days)", _WINDOW_DAYS)

    # ── 1. Read all events within the collection window ────────────────────────
    window_start = (
        datetime.now(timezone.utc) - timedelta(days=_WINDOW_DAYS)
    ).isoformat()

    items = _scan_events(window_start)
    logger.info("Scanned %d event records from DynamoDB", len(items))

    # ── 2. Load previous state for delta computation ──────────────────────────
    prev_open_arns = _load_json_from_s3(_STATE_KEY, default=[])
    delta_log_rows = _load_json_from_s3(_DELTA_LOG_KEY, default=[])

    # ── 3. Generate Excel ─────────────────────────────────────────────────────
    xlsx_bytes = write_excel(
        events=items,
        prev_open_arns=prev_open_arns,
        delta_log_rows=delta_log_rows,
    )

    # ── 4. Upload Excel to S3 ─────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    report_key = f"exports/{now.year}/{now.month:02d}/{now.day:02d}/aws-health-events.xlsx"

    _s3.put_object(
        Bucket=_EXPORT_BUCKET,
        Key=report_key,
        Body=xlsx_bytes,
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ServerSideEncryption="aws:kms",
        Metadata={
            "generated-utc": now.isoformat(),
            "event-count": str(len(items)),
            "window-days": str(_WINDOW_DAYS),
        },
    )
    logger.info("Uploaded report: s3://%s/%s (%d bytes)", _EXPORT_BUCKET, report_key, len(xlsx_bytes))

    # ── 5. Persist state for next run's delta ─────────────────────────────────
    new_open_arns = current_open_arns(items)
    _save_json_to_s3(_STATE_KEY, new_open_arns)

    # Append delta log entries (keep last 90 days / 90 runs)
    from excel_writer import _build_dataframes, _compute_delta, _build_delta_log
    import pandas as pd
    events_df, _ = _build_dataframes(items)
    delta_new_df, delta_resolved_df = _compute_delta(events_df, prev_open_arns)
    updated_log_df = _build_delta_log(delta_new_df, delta_resolved_df, delta_log_rows)
    recent_rows = updated_log_df.tail(2000).to_dict("records")  # cap history
    _save_json_to_s3(_DELTA_LOG_KEY, recent_rows)

    result = {
        "statusCode": 200,
        "report_s3_key": report_key,
        "events_exported": len(items),
        "new_open_events": int((events_df["status"] == "open").sum()) if not events_df.empty else 0,
        "delta_new": len(delta_new_df),
        "delta_resolved": len(delta_resolved_df),
    }
    logger.info("Export complete: %s", result)
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scan_events(window_start: str) -> list:
    """Scan all DynamoDB event items updated within the window."""
    items = []
    kwargs = {
        "FilterExpression": Attr("last_updated_time").gte(window_start),
    }
    while True:
        resp = _table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _load_json_from_s3(key: str, default):
    try:
        obj = _s3.get_object(Bucket=_EXPORT_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except _s3.exceptions.NoSuchKey:
        return default
    except Exception as exc:
        logger.warning("Failed to load state from s3://%s/%s: %s", _EXPORT_BUCKET, key, exc)
        return default


def _save_json_to_s3(key: str, data) -> None:
    try:
        _s3.put_object(
            Bucket=_EXPORT_BUCKET,
            Key=key,
            Body=json.dumps(data, default=str).encode(),
            ContentType="application/json",
            ServerSideEncryption="aws:kms",
        )
    except Exception as exc:
        logger.warning("Failed to save state to s3://%s/%s: %s", _EXPORT_BUCKET, key, exc)
