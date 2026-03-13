"""
handler.py — Collector Lambda entry point.

Triggered every 15 minutes by EventBridge. For each configured org:
  1. Assumes the org's delegated admin role (via STS VPC endpoint) to access
     the Organizations API for account metadata.
  2. Loads / refreshes account metadata from DynamoDB cache.
  3. Calls the Health Proxy API Gateway for events (execute-api VPC endpoint).
     The proxy's integration role calls health.us-east-1.amazonaws.com.
  4. Calls the proxy for affected accounts per event.
  5. Enriches events with account metadata and upserts to DynamoDB.
  6. Writes collection state (last run, error, count) to the state table.

Multi-org note:
  The Health Proxy API GW uses a fixed IAM integration role. That role must be
  (or have been delegated as) the Health org admin for the org whose events
  you want. For a single org where the aggregator account is the delegated
  admin this works out of the box. For truly separate AWS Organizations, each
  org needs its own proxy deployment — see SPEC.md §10, Pattern C.
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import boto3

from account_cache import load_account_map
from health_proxy_client import HealthProxyClient, HealthAPIError
from org_registry import load_orgs

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Environment ───────────────────────────────────────────────────────────────
_TABLE_NAME = os.environ["TABLE_NAME"]
_STATE_TABLE_NAME = os.environ["STATE_TABLE_NAME"]
_HEALTH_PROXY_URL = os.environ["HEALTH_PROXY_API_URL"]
_WINDOW_DAYS = int(os.environ.get("COLLECTION_WINDOW_DAYS", "7"))
_MAX_CONCURRENT_ORGS = int(os.environ.get("MAX_CONCURRENT_ORGS", "5"))
_REGION = os.environ.get("AWS_REGION", "us-east-1")

# ── AWS clients (module-level for Lambda warm reuse) ─────────────────────────
_dynamodb = boto3.resource("dynamodb")
_events_table = _dynamodb.Table(_TABLE_NAME)
_state_table = _dynamodb.Table(_STATE_TABLE_NAME)
_sts = boto3.client("sts")
_cloudwatch = boto3.client("cloudwatch")

# ── Categories to collect ─────────────────────────────────────────────────────
_CATEGORIES = ["issue", "investigation"]


def handler(event: dict, context) -> dict:
    orgs = load_orgs()
    window_start = (
        datetime.now(timezone.utc) - timedelta(days=_WINDOW_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    health_client = HealthProxyClient(
        api_base_url=_HEALTH_PROXY_URL,
        region=_REGION,
    )

    total_events = 0
    errors: list = []

    with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_ORGS) as pool:
        futures = {
            pool.submit(_collect_org, org, health_client, window_start): org
            for org in orgs
        }
        for future in as_completed(futures):
            org = futures[future]
            try:
                count = future.result()
                total_events += count
                logger.info("org=%s collected=%d", org["org_id"], count)
            except Exception as exc:
                msg = str(exc)
                errors.append({"org_id": org["org_id"], "error": msg})
                logger.error("org=%s error: %s", org["org_id"], msg)
                _put_collection_state(org, success=False, error=msg, count=0)
                _emit_metric("CollectionErrors", 1, org["org_id"])

    _emit_metric("EventsCollected", total_events)
    logger.info("Collection complete: %d events, %d org errors", total_events, len(errors))

    if errors:
        return {"statusCode": 207, "errors": errors, "total_events": total_events}
    return {"statusCode": 200, "total_events": total_events}


def _collect_org(org: dict, health_client: HealthProxyClient, window_start: str) -> int:
    """
    Full collection pipeline for a single org.
    Returns the number of event records written to DynamoDB.
    """
    org_id = org["org_id"]
    org_name = org["org_name"]
    t0 = time.time()

    # Step 1: assume org role for Organizations API access
    assumed = _assume_org_role(org)

    # Step 2: build account map from cache (Organizations API on miss)
    account_map = load_account_map(org_id, assumed)  # {account_id: {name,bu,env}}

    # Step 3: fetch events via Health Proxy API GW (uses proxy's own IAM role)
    events = health_client.describe_events_for_organization(
        categories=_CATEGORIES,
        last_updated_from=window_start,
    )

    # Step 4: enrich events with affected accounts and write to DynamoDB
    records_written = 0
    for ev in events:
        try:
            records_written += _process_event(ev, org_id, org_name, account_map, health_client)
        except Exception as exc:
            logger.warning("Skipping event %s: %s", ev.get("arn", "?"), exc)

    duration_ms = int((time.time() - t0) * 1000)
    _emit_metric("OrgCollectionDurationMs", duration_ms, org_id)
    _put_collection_state(org, success=True, error=None, count=records_written)

    return records_written


def _process_event(
    ev: dict,
    org_id: str,
    org_name: str,
    account_map: dict,
    health_client: HealthProxyClient,
) -> int:
    """
    Fetch affected accounts for one event, filter to known org accounts,
    and upsert one DynamoDB item per (event, org).

    Returns 1 if an item was written, 0 if no org accounts were affected.
    """
    event_arn = ev["arn"]
    category = ev.get("eventTypeCategory", "issue")
    start_time = _iso(ev.get("startTime"))

    # Get all affected account IDs for this event (paginated via proxy)
    all_affected = health_client.describe_affected_accounts_for_organization(event_arn)

    # Keep only accounts that belong to this org (in our account_map)
    org_accounts = [
        {
            "account_id": acct_id,
            **account_map[acct_id],
        }
        for acct_id in all_affected
        if acct_id in account_map
    ]

    if not org_accounts:
        return 0  # event does not affect any account in this org

    now_epoch = int(time.time())
    ttl = now_epoch + (_WINDOW_DAYS * 24 * 3600)

    item = {
        # Keys
        "pk": f"{event_arn}#{org_id}",
        "sk": f"{category}#{start_time}",
        # Event fields
        "event_arn": event_arn,
        "org_id": org_id,
        "org_name": org_name,
        "category": category,
        "service": ev.get("service", ""),
        "event_type_code": ev.get("eventTypeCode", ""),
        "region": ev.get("region", "global"),
        "status": ev.get("statusCode", "open"),
        "start_time": start_time,
        "last_updated_time": _iso(ev.get("lastUpdatedTime")),
        "end_time": _iso(ev.get("endTime")) if ev.get("endTime") else None,
        # Enriched account data
        "affected_accounts": org_accounts,
        "affected_account_count": len(org_accounts),
        # Housekeeping
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "ttl": ttl,
    }
    # Remove None values — DynamoDB rejects explicit nulls on non-nullable attrs
    item = {k: v for k, v in item.items() if v is not None}

    _events_table.put_item(Item=item)
    return 1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _assume_org_role(org: dict) -> dict:
    """Assume the org's delegated admin IAM role and return raw credentials."""
    kwargs = {
        "RoleArn": org["assume_role_arn"],
        "RoleSessionName": f"health-aggregator-{org['org_id']}",
        "DurationSeconds": 900,
    }
    if org.get("assume_role_external_id"):
        kwargs["ExternalId"] = org["assume_role_external_id"]

    resp = _sts.assume_role(**kwargs)
    return resp["Credentials"]  # AccessKeyId, SecretAccessKey, SessionToken


def _iso(dt_value) -> str:
    """Normalise a datetime (object or string) to an ISO 8601 UTC string."""
    if dt_value is None:
        return ""
    if hasattr(dt_value, "isoformat"):
        if dt_value.tzinfo is None:
            dt_value = dt_value.replace(tzinfo=timezone.utc)
        return dt_value.isoformat()
    return str(dt_value)


def _put_collection_state(
    org: dict,
    success: bool,
    error: str | None,
    count: int,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    item = {
        "pk": org["org_id"],
        "org_id": org["org_id"],
        "org_name": org["org_name"],
        "last_attempted_at": now_iso,
        "events_in_window": count,
        "updated_at": now_iso,
    }
    if success:
        item["last_successful_at"] = now_iso
        item.pop("last_error", None)
    else:
        item["last_error"] = error or "unknown"

    _state_table.put_item(Item=item)


def _emit_metric(metric_name: str, value: float, org_id: str = "all") -> None:
    try:
        _cloudwatch.put_metric_data(
            Namespace="HealthAggregator",
            MetricData=[{
                "MetricName": metric_name,
                "Dimensions": [{"Name": "OrgId", "Value": org_id}],
                "Value": value,
                "Unit": "Count" if metric_name != "OrgCollectionDurationMs" else "Milliseconds",
            }],
        )
    except Exception as exc:
        logger.warning("Failed to emit metric %s: %s", metric_name, exc)
