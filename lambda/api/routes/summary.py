"""
routes/summary.py — GET /v1/summary
"""

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

_TABLE_NAME = os.environ["TABLE_NAME"]
_WINDOW_DAYS = int(os.environ.get("COLLECTION_WINDOW_DAYS", "7"))

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME)


def get_summary(query: dict, _multi: dict, _param=None) -> dict:
    category_filter = query.get("category", "all")
    org_id_filter = query.get("org_id")
    window_days = min(int(query.get("window_days", _WINDOW_DAYS)), _WINDOW_DAYS)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    window_start_str = window_start.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    categories = (
        ["issue", "investigation"] if category_filter == "all" else [category_filter]
    )

    all_items: list = []
    for cat in categories:
        kwargs = {
            "IndexName": "category-starttime-index",
            "KeyConditionExpression": (
                Key("category").eq(cat) & Key("start_time").gte(window_start_str)
            ),
        }
        while True:
            resp = _table.query(**kwargs)
            all_items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    if org_id_filter:
        all_items = [i for i in all_items if i.get("org_id") == org_id_filter]

    # Aggregate
    issues = defaultdict(int)
    investigations = defaultdict(int)
    by_org: dict = {}
    service_counter: Counter = Counter()
    region_counter: Counter = Counter()
    affected_account_ids: set = set()

    for item in all_items:
        cat = item.get("category")
        status = item.get("status", "open")
        oid = item.get("org_id", "unknown")
        oname = item.get("org_name", "unknown")

        counter = issues if cat == "issue" else investigations
        counter["total"] += 1
        counter[status] += 1

        if oid not in by_org:
            by_org[oid] = {
                "org_id": oid,
                "org_name": oname,
                "issues": defaultdict(int),
                "investigations": defaultdict(int),
            }
        org_counter = by_org[oid]["issues" if cat == "issue" else "investigations"]
        org_counter[status] += 1

        service_counter[item.get("service", "Unknown")] += 1
        region_counter[item.get("region", "global")] += 1

        for acct in item.get("affected_accounts", []):
            affected_account_ids.add(acct.get("account_id"))

    # Serialise org breakdown
    org_list = []
    for oid, data in by_org.items():
        org_list.append({
            "org_id": oid,
            "org_name": data["org_name"],
            "issues": dict(data["issues"]),
            "investigations": dict(data["investigations"]),
        })

    summary = {
        "issues": dict(issues),
        "investigations": dict(investigations),
        "by_org": org_list,
        "top_affected_services": [
            {"service": s, "event_count": c}
            for s, c in service_counter.most_common(10)
        ],
        "top_affected_regions": [
            {"region": r, "event_count": c}
            for r, c in region_counter.most_common(10)
        ],
        "affected_account_count": len(affected_account_ids),
    }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({
            "meta": {
                "window_start": window_start.isoformat(),
                "window_end": now.isoformat(),
                "window_days": window_days,
            },
            "summary": summary,
        }, default=str),
    }
