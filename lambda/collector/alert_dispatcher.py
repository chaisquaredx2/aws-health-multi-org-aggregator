"""
alert_dispatcher.py — Digest mode with service-level incident correlation.

How it works
------------
1. CORRELATION: New alertable events are grouped into "incidents" by
   (service, start_time_bucket). Two events belong to the same incident
   if they share the same service and their start_times fall within the
   same CORRELATION_WINDOW_MINUTES bucket. This collapses the storm of
   per-ARN / per-region events AWS emits during a large outage into a
   single incident record stored in the collection_state DynamoDB table.

2. DIGEST: Incidents accumulate for DIGEST_WINDOW_MINUTES before the
   first alert is sent. A 30-min window means a 15-min collector cycle
   fires twice before the alert goes out — by then most related ARNs
   from the same outage are already correlated into one message.

3. RE-ALERT: After the initial digest fires, subsequent alerts are
   suppressed unless:
     - affected_account_count doubles since the last alert, OR
     - new regions are added (outage is spreading)

Configuration env vars
----------------------
  HEALTH_ALERT_SNS_TOPIC_ARN      — SNS topic ARN; if empty alerting is a no-op
  ALERTS_ENABLED                  — "true" (default) or "false"
  DIGEST_WINDOW_MINUTES           — minutes to accumulate before first alert (default 30)
  CORRELATION_WINDOW_MINUTES      — minutes window for grouping same-service events (default 60)
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_SNS_TOPIC_ARN              = os.environ.get("HEALTH_ALERT_SNS_TOPIC_ARN", "")
_ALERTS_ENABLED             = os.environ.get("ALERTS_ENABLED", "true").lower() == "true"
_DIGEST_WINDOW_MINUTES      = int(os.environ.get("DIGEST_WINDOW_MINUTES", "30"))
_CORRELATION_WINDOW_MINUTES = int(os.environ.get("CORRELATION_WINDOW_MINUTES", "60"))

_sns = boto3.client("sns")


# ── Public API ────────────────────────────────────────────────────────────────

def dispatch(events: List[dict], state_table) -> int:
    """
    Correlate new events into incidents, then flush mature incident digests.
    Returns the number of SNS messages published this cycle.
    """
    if not _ALERTS_ENABLED or not _SNS_TOPIC_ARN:
        return 0

    alertable = [
        e for e in events
        if e.get("status") == "open" and e.get("is_operational", False)
    ]

    if alertable:
        _correlate_events(alertable, state_table)

    return _flush_digests(state_table)


# ── Incident correlation ──────────────────────────────────────────────────────

def _start_bucket(start_time_str: str) -> str:
    """Floor a start_time ISO string to the nearest CORRELATION_WINDOW_MINUTES bucket."""
    try:
        dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        dt = datetime.now(timezone.utc)
    window_secs = _CORRELATION_WINDOW_MINUTES * 60
    bucket_ts   = (int(dt.timestamp()) // window_secs) * window_secs
    return datetime.fromtimestamp(bucket_ts, tz=timezone.utc).strftime("%Y%m%dT%H%M")


def _incident_pk(service: str, bucket: str) -> str:
    return f"incident#{service}#{bucket}"


def _correlate_events(events: List[dict], state_table) -> None:
    """Group events by (service, start_bucket) and upsert incidents."""
    groups: dict = {}
    for ev in events:
        service = ev.get("service", "Unknown")
        bucket  = _start_bucket(ev.get("start_time", ""))
        groups.setdefault((service, bucket), []).append(ev)

    now_iso = datetime.now(timezone.utc).isoformat()

    for (service, bucket), group_events in groups.items():
        pk = _incident_pk(service, bucket)

        try:
            resp     = state_table.get_item(Key={"pk": pk})
            incident = resp.get("Item") or _new_incident(pk, service, bucket, now_iso)
        except Exception as exc:
            logger.warning("Failed to load incident %s: %s", pk, exc)
            incident = _new_incident(pk, service, bucket, now_iso)

        for ev in group_events:
            _merge_event(incident, ev)

        incident["last_updated"] = now_iso
        incident["event_count"]  = len(incident["event_arns"])

        try:
            state_table.put_item(Item=_to_dynamo(incident))
        except Exception as exc:
            logger.warning("Failed to save incident %s: %s", pk, exc)


def _new_incident(pk: str, service: str, bucket: str, now_iso: str) -> dict:
    return {
        "pk":                         pk,
        "service":                    service,
        "start_bucket":               bucket,
        "event_arns":                 [],
        "regions":                    [],
        "org_ids":                    [],
        "severities":                 [],
        "event_type_codes":           [],
        "affected_account_count":     0,
        "event_count":                0,
        "first_seen":                 now_iso,
        "last_updated":               now_iso,
        "last_alerted_account_count": 0,
        "last_alerted_regions":       [],
    }


def _merge_event(incident: dict, ev: dict) -> None:
    """Merge a single event into the incident, deduplicating list fields."""
    def _add(lst: list, val) -> None:
        if val and val not in lst:
            lst.append(val)

    _add(incident["event_arns"],       ev.get("event_arn"))
    _add(incident["regions"],          ev.get("region"))
    _add(incident["org_ids"],          ev.get("org_id"))
    _add(incident["severities"],       ev.get("severity"))
    _add(incident["event_type_codes"], ev.get("event_type_code"))

    # Take the maximum affected_account_count seen across events
    incident["affected_account_count"] = max(
        int(incident.get("affected_account_count", 0)),
        int(ev.get("affected_account_count", 0)),
    )


def _to_dynamo(incident: dict) -> dict:
    """Convert in-memory incident to a DynamoDB-safe dict (strip None and empty lists)."""
    return {k: v for k, v in incident.items() if v is not None and v != [] and v != ""}


# ── Digest flush ──────────────────────────────────────────────────────────────

def _flush_digests(state_table) -> int:
    """Scan collection_state for pending incidents and send mature digests."""
    now           = datetime.now(timezone.utc)
    digest_cutoff = (now - timedelta(minutes=_DIGEST_WINDOW_MINUTES)).isoformat()

    try:
        resp      = state_table.scan(FilterExpression=Attr("pk").begins_with("incident#"))
        incidents = resp.get("Items", [])
    except Exception as exc:
        logger.error("Failed to scan incidents: %s", exc)
        return 0

    sent = 0
    for incident in incidents:
        first_seen     = incident.get("first_seen", now.isoformat())
        alert_sent_at  = incident.get("alert_sent_at")
        is_mature      = first_seen <= digest_cutoff
        is_first_alert = alert_sent_at is None
        is_update      = (not is_first_alert) and _has_significant_update(incident)

        if is_mature and (is_first_alert or is_update):
            if _send_digest(incident, is_update=not is_first_alert):
                _mark_alerted(incident, state_table, now.isoformat())
                sent += 1

    return sent


def _has_significant_update(incident: dict) -> bool:
    """Re-alert if account count doubled or new regions added since last alert."""
    last_count    = int(incident.get("last_alerted_account_count", 0))
    current_count = int(incident.get("affected_account_count", 0))
    last_regions  = set(incident.get("last_alerted_regions", []))
    curr_regions  = set(incident.get("regions", []))

    if last_count > 0 and current_count >= last_count * 2:
        return True
    if curr_regions - last_regions:
        return True
    return False


def _send_digest(incident: dict, is_update: bool = False) -> bool:
    """Format and publish a single SNS digest for one incident."""
    service           = incident.get("service", "Unknown")
    regions           = sorted(incident.get("regions", []))
    event_count       = int(incident.get("event_count", 1))
    affected_accounts = int(incident.get("affected_account_count", 0))
    severities        = incident.get("severities", [])
    codes             = sorted(incident.get("event_type_codes", []))
    org_count         = len(incident.get("org_ids", []))

    is_multi_region = len(regions) >= 2
    priority        = "HIGH" if (is_multi_region and affected_accounts > 100) else "STANDARD"
    alert_type      = "UPDATE" if is_update else "NEW INCIDENT"
    priority_label  = "🔴 HIGH" if priority == "HIGH" else "⚠️  ALERT"

    subject = (
        f"{priority_label} [{alert_type}]: {service} — "
        f"{event_count} event(s), {len(regions)} region(s), {affected_accounts} accounts"
    )[:100]

    lines = [
        "AWS Health Aggregator — Incident Digest",
        "",
        f"Type      : {alert_type}",
        f"Priority  : {priority}",
        f"Service   : {service}",
        f"Regions   : {', '.join(regions) or 'global'}",
        f"Events    : {event_count} correlated event(s)",
        f"Accounts  : {affected_accounts}",
        f"Severity  : {', '.join(sorted(severities))}",
        f"Orgs      : {org_count} org(s)",
        f"First seen: {incident.get('first_seen', '')}",
        f"Last seen : {incident.get('last_updated', '')}",
        "",
        "Event types:",
    ]
    for code in codes[:10]:
        lines.append(f"  - {code}")
    if len(codes) > 10:
        lines.append(f"  ... and {len(codes) - 10} more")

    lines += [
        "",
        "---",
        "JSON payload:",
        json.dumps({
            "alert_type":        "aws_health_incident_digest",
            "is_update":         is_update,
            "priority":          priority,
            "service":           service,
            "event_count":       event_count,
            "affected_accounts": affected_accounts,
            "regions":           regions,
            "severities":        sorted(severities),
            "event_type_codes":  codes,
            "org_count":         org_count,
            "first_seen":        incident.get("first_seen"),
            "incident_id":       incident.get("pk"),
        }, indent=2),
    ]

    try:
        _sns.publish(
            TopicArn=_SNS_TOPIC_ARN,
            Subject=subject,
            Message="\n".join(lines),
            MessageAttributes={
                "priority":          {"DataType": "String", "StringValue": priority},
                "service":           {"DataType": "String", "StringValue": service},
                "affected_accounts": {"DataType": "Number", "StringValue": str(affected_accounts)},
                "regions":           {"DataType": "String", "StringValue": ",".join(regions)},
                "alert_type":        {"DataType": "String", "StringValue": alert_type},
            },
        )
        logger.info(
            "Digest sent: service=%s regions=%s events=%d accounts=%d priority=%s update=%s",
            service, regions, event_count, affected_accounts, priority, is_update,
        )
        return True
    except ClientError as exc:
        logger.error("Failed to publish digest for %s: %s", incident.get("pk"), exc)
        return False


def _mark_alerted(incident: dict, state_table, now_iso: str) -> None:
    """Snapshot alert state so we can detect significant updates next cycle."""
    try:
        state_table.update_item(
            Key={"pk": incident["pk"]},
            UpdateExpression=(
                "SET alert_sent_at = :ts, "
                "last_alerted_account_count = :ac, "
                "last_alerted_regions = :regions"
            ),
            ExpressionAttributeValues={
                ":ts":      now_iso,
                ":ac":      int(incident.get("affected_account_count", 0)),
                ":regions": incident.get("regions", []),
            },
        )
    except Exception as exc:
        logger.warning("Failed to mark incident alerted %s: %s", incident.get("pk"), exc)
