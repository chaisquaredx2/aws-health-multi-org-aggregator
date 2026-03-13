"""
routes/events.py — GET /v1/events and GET /v1/events/{arn_b64}/details
"""

import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

_TABLE_NAME = os.environ["TABLE_NAME"]
_WINDOW_DAYS = int(os.environ.get("COLLECTION_WINDOW_DAYS", "7"))
_HEALTH_PROXY_URL = os.environ.get("HEALTH_PROXY_API_URL", "")

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME)

_VALID_CATEGORIES = {"issue", "investigation"}
_VALID_STATUSES = {"open", "closed", "upcoming"}
_VALID_ENVIRONMENTS = {"production", "non-production"}


# ── GET /v1/events ────────────────────────────────────────────────────────────

def list_events(query: dict, multi_query: dict, _path_param=None) -> dict:
    category = query.get("category", "")
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(sorted(_VALID_CATEGORIES))}")

    window_days = int(query.get("window_days", _WINDOW_DAYS))
    if not 1 <= window_days <= _WINDOW_DAYS:
        raise ValueError(f"window_days must be between 1 and {_WINDOW_DAYS}")

    page_size = min(int(query.get("page_size", 100)), 200)
    next_token_raw = query.get("next_token")
    org_id_filter = query.get("org_id")
    service_filter = query.get("service")
    region_filter = query.get("region")
    env_filter = query.get("environment")
    status_filters = set(multi_query.get("status") or ([query["status"]] if "status" in query else []))
    if status_filters - _VALID_STATUSES:
        raise ValueError(f"status values must be from: {', '.join(sorted(_VALID_STATUSES))}")

    window_start, window_end = _window_bounds(window_days)
    window_start_str = window_start.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # Query GSI: category-starttime-index
    kwargs = {
        "IndexName": "category-starttime-index",
        "KeyConditionExpression": (
            Key("category").eq(category) & Key("start_time").gte(window_start_str)
        ),
        "Limit": page_size,
    }
    if next_token_raw:
        kwargs["ExclusiveStartKey"] = json.loads(
            base64.b64decode(next_token_raw).decode()
        )

    resp = _table.query(**kwargs)
    items = resp.get("Items", [])

    # Apply in-memory filters (DynamoDB FilterExpression would consume capacity
    # before Limit is applied; do it here for correctness)
    if org_id_filter:
        items = [i for i in items if i.get("org_id") == org_id_filter]
    if service_filter:
        items = [i for i in items if i.get("service", "").upper() == service_filter.upper()]
    if region_filter:
        items = [i for i in items if i.get("region") == region_filter]
    if status_filters:
        items = [i for i in items if i.get("status") in status_filters]
    if env_filter:
        items = [
            i for i in items
            if any(
                a.get("environment") == env_filter
                for a in i.get("affected_accounts", [])
            )
        ]

    # Merge records with the same event_arn across orgs
    merged = _merge_by_arn(items)

    # Build next_token
    lek = resp.get("LastEvaluatedKey")
    new_next_token = (
        base64.b64encode(json.dumps(lek).encode()).decode() if lek else None
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({
            "meta": {
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "window_days": window_days,
                "total": len(merged),
                "returned": len(merged),
                "next_token": new_next_token,
            },
            "data": merged,
        }, default=str),
    }


# ── GET /v1/events/{arn_b64}/details ─────────────────────────────────────────

def get_event_details(query: dict, _multi_query: dict, path_param: str) -> dict:
    try:
        event_arn = base64.urlsafe_b64decode(
            path_param + "=" * (4 - len(path_param) % 4)
        ).decode()
    except Exception:
        raise ValueError("event_arn_b64 is not valid base64url")

    org_id_filter = query.get("org_id")

    # Fetch all org records for this event_arn from the table
    # Using begins_with on pk (scan is acceptable for single-event lookup)
    resp = _table.scan(
        FilterExpression="begins_with(pk, :prefix)",
        ExpressionAttributeValues={":prefix": f"{event_arn}#"},
    )
    items = resp.get("Items", [])

    if not items:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": {"code": "NOT_FOUND", "message": "Event not found"}}),
        }

    if org_id_filter:
        items = [i for i in items if i.get("org_id") == org_id_filter]

    merged = _merge_by_arn(items)
    if not merged:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": {"code": "NOT_FOUND", "message": "Event not found for org"}}),
        }

    detail = merged[0]

    # Fetch live description from Health proxy (Phase 2 feature)
    # Description is fetched using the first available org's proxy credentials
    if _HEALTH_PROXY_URL:
        description = _fetch_description(event_arn, items[0].get("org_id"))
        detail["description"] = description

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(detail, default=str),
    }


# ── Shared helpers ────────────────────────────────────────────────────────────

def _merge_by_arn(items: list) -> list:
    """
    Merge DynamoDB items that share the same event_arn into one response
    object with an affected_orgs[] array (one entry per org).

    Items from the same org for the same ARN are collapsed (shouldn't happen
    given PK design, but handled defensively).
    """
    by_arn: dict = {}

    for item in items:
        arn = item["event_arn"]
        if arn not in by_arn:
            by_arn[arn] = {
                "event_arn": arn,
                "category": item.get("category"),
                "service": item.get("service"),
                "event_type_code": item.get("event_type_code"),
                "region": item.get("region"),
                "status": item.get("status"),
                "start_time": item.get("start_time"),
                "last_updated_time": item.get("last_updated_time"),
                "end_time": item.get("end_time"),
                "affected_account_count": 0,
                "affected_orgs": [],
            }

        entry = by_arn[arn]

        # Keep the most-recently-updated metadata
        if item.get("last_updated_time", "") > entry.get("last_updated_time", ""):
            entry["status"] = item.get("status")
            entry["last_updated_time"] = item.get("last_updated_time")
            entry["end_time"] = item.get("end_time")

        accounts = item.get("affected_accounts", [])
        entry["affected_account_count"] += len(accounts)
        entry["affected_orgs"].append({
            "org_id": item.get("org_id"),
            "org_name": item.get("org_name"),
            "affected_accounts": accounts,
        })

    return list(by_arn.values())


def _window_bounds(window_days: int):
    now = datetime.now(timezone.utc)
    return now - timedelta(days=window_days), now


def _fetch_description(event_arn: str, org_id: str) -> dict:
    """
    Fetch event description text live from the Health Proxy API GW.
    Called only by the details endpoint. Returns description dict or empty.
    """
    try:
        from health_proxy_client import HealthProxyClient
        client = HealthProxyClient(api_base_url=_HEALTH_PROXY_URL)
        result = client.describe_event_details_for_organization([event_arn])
        for entry in result.get("successfulSet", []):
            desc = entry.get("eventDescription", {})
            if desc.get("latestDescription"):
                return {
                    "latest_description": desc["latestDescription"],
                    "description_updated_at": entry.get("event", {}).get(
                        "lastUpdatedTime", ""
                    ),
                    "fetched_from_org_id": org_id,
                }
    except Exception as exc:
        logger.warning("Failed to fetch description for %s: %s", event_arn, exc)
    return {}
