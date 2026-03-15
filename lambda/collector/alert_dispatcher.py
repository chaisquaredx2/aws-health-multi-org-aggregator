"""
alert_dispatcher.py

Sends health event alerts via SNS after each collection cycle.

Alert criteria (ported from aws-health-monitor):
  - Trigger if: any operational event is in us-east-1 OR events span 2+ regions
  - Priority HIGH if: multi-region AND affected_account_count > 100 total
  - Deduplication: skips events already alerted with the same status (checks
    collection_state table for alert_sent_at)

Delivery model:
  Lambda is VPC-attached with no internet egress. We publish to SNS via the
  SNS VPC endpoint. SNS then delivers from AWS-managed network to subscribers
  (email, PagerDuty HTTPS subscription, Slack, etc.) without requiring Lambda
  to reach the internet.

Configuration env vars:
  HEALTH_ALERT_SNS_TOPIC_ARN  — SNS topic ARN; if empty, alerting is a no-op
  ALERTS_ENABLED              — "true" (default) or "false"
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_SNS_TOPIC_ARN = os.environ.get("HEALTH_ALERT_SNS_TOPIC_ARN", "")
_ALERTS_ENABLED = os.environ.get("ALERTS_ENABLED", "true").lower() == "true"

_sns = boto3.client("sns")


def dispatch(
    events: List[dict],
    state_table,
) -> int:
    """
    Evaluate collected events for alert criteria and publish to SNS.

    Args:
        events:      List of DynamoDB event items written this cycle.
                     Each item is the dict passed to _events_table.put_item.
        state_table: boto3 DynamoDB Table resource for collection_state.

    Returns:
        Number of SNS messages published (0 if no alert criteria met).
    """
    if not _ALERTS_ENABLED:
        logger.debug("Alerting disabled (ALERTS_ENABLED=false)")
        return 0

    if not _SNS_TOPIC_ARN:
        logger.debug("No HEALTH_ALERT_SNS_TOPIC_ARN set; skipping alerts")
        return 0

    # Only alert on open, operational, events
    alertable = [
        e for e in events
        if e.get("status") == "open" and e.get("is_operational", False)
    ]

    if not alertable:
        return 0

    # Dedup: skip events already alerted with the same status
    new_events = _filter_already_alerted(alertable, state_table)
    if not new_events:
        logger.debug("All %d alertable events already alerted; skipping", len(alertable))
        return 0

    # Check alert trigger criteria
    if not _should_alert(new_events):
        logger.debug("Alert criteria not met (%d events); no SNS publish", len(new_events))
        return 0

    # Determine priority and channel
    affected_accounts = sum(e.get("affected_account_count", 0) for e in new_events)
    unique_regions = {e.get("region") for e in new_events if e.get("region")}
    is_multi_region = len(unique_regions) >= 2
    priority = "HIGH" if (is_multi_region and affected_accounts > 100) else "STANDARD"

    subject, message = _format_message(new_events, affected_accounts, unique_regions, priority)

    try:
        _sns.publish(
            TopicArn=_SNS_TOPIC_ARN,
            Subject=subject[:100],  # SNS subject max 100 chars
            Message=message,
            MessageAttributes={
                "priority": {"DataType": "String", "StringValue": priority},
                "affected_accounts": {"DataType": "Number", "StringValue": str(affected_accounts)},
                "regions": {"DataType": "String", "StringValue": ",".join(sorted(unique_regions))},
            },
        )
        logger.info(
            "Alert published to SNS: priority=%s events=%d accounts=%d regions=%s",
            priority, len(new_events), affected_accounts, sorted(unique_regions),
        )
        _mark_alerted(new_events, state_table)
        return 1

    except ClientError as exc:
        logger.error("Failed to publish alert to SNS: %s", exc)
        return 0


def _should_alert(events: List[dict]) -> bool:
    """Alert if any event is in us-east-1 OR events span 2+ regions."""
    if any(e.get("region") == "us-east-1" for e in events):
        return True
    unique_regions = {e.get("region") for e in events if e.get("region")}
    return len(unique_regions) >= 2


def _filter_already_alerted(events: List[dict], state_table) -> List[dict]:
    """
    Return only events not already alerted with the current status.
    Checks the collection_state table for each event_arn.
    """
    new_events = []
    for ev in events:
        event_arn = ev.get("event_arn", "")
        current_status = ev.get("status", "")
        try:
            resp = state_table.get_item(Key={"pk": ev.get("org_id", "")})
            # The state table is keyed by org_id; per-event alert state is stored
            # in a separate key format: alert#<event_arn>
            alert_resp = state_table.get_item(Key={"pk": f"alert#{event_arn}"})
            item = alert_resp.get("Item")
            if item is None:
                new_events.append(ev)
            elif item.get("alerted_status") != current_status:
                # Status changed (e.g., reopened) — re-alert
                new_events.append(ev)
        except Exception as exc:
            logger.warning("Failed to check alert state for %s: %s", event_arn, exc)
            new_events.append(ev)  # include on error (prefer false positive)

    return new_events


def _mark_alerted(events: List[dict], state_table) -> None:
    """Write alert_sent_at to collection_state table for each event ARN."""
    now_iso = datetime.now(timezone.utc).isoformat()
    for ev in events:
        event_arn = ev.get("event_arn", "")
        try:
            state_table.put_item(Item={
                "pk": f"alert#{event_arn}",
                "event_arn": event_arn,
                "alerted_status": ev.get("status", ""),
                "alerted_severity": ev.get("severity", "standard"),
                "alert_sent_at": now_iso,
            })
        except Exception as exc:
            logger.warning("Failed to mark alert state for %s: %s", event_arn, exc)


def _format_message(
    events: List[dict],
    affected_accounts: int,
    regions: set,
    priority: str,
) -> tuple:
    """Format SNS subject and body."""
    priority_label = "🔴 HIGH PRIORITY" if priority == "HIGH" else "⚠️  Alert"
    region_list = ", ".join(sorted(regions))

    # Group by service
    by_service: dict = {}
    for ev in events:
        svc = ev.get("service", "Unknown")
        by_service.setdefault(svc, []).append(ev)

    if len(events) == 1:
        ev = events[0]
        subject = f"{priority_label}: AWS Health — {ev.get('service')} {ev.get('event_type_code', '')} in {ev.get('region')}"
    else:
        subject = (
            f"{priority_label}: AWS Health — {len(events)} events across "
            f"{len(regions)} region(s) ({', '.join(sorted({e.get('service','?') for e in events})[:3])}...)"
        )

    lines = [
        "AWS Health Aggregator Alert",
        f"Priority : {priority}",
        f"Events   : {len(events)}",
        f"Accounts : {affected_accounts}",
        f"Regions  : {region_list}",
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Event Details:",
        "",
    ]

    for svc, svc_events in sorted(by_service.items()):
        lines.append(f"Service: {svc} ({len(svc_events)} event(s))")
        for ev in svc_events:
            severity = ev.get("severity", "standard").upper()
            lines.append(f"  [{severity}] {ev.get('event_type_code', 'N/A')}")
            lines.append(f"    ARN    : {ev.get('event_arn', '')}")
            lines.append(f"    Org    : {ev.get('org_name', ev.get('org_id', ''))}")
            lines.append(f"    Region : {ev.get('region', '')}")
            lines.append(f"    Status : {ev.get('status', '')}")
            lines.append(f"    Started: {ev.get('start_time', '')}")
            lines.append(f"    Accts  : {ev.get('affected_account_count', 0)}")
            lines.append("")

    # Also include compact JSON payload for programmatic subscribers
    lines += [
        "---",
        "JSON payload:",
        json.dumps({
            "alert_type": "aws_health_event",
            "priority": priority,
            "event_count": len(events),
            "affected_accounts": affected_accounts,
            "regions": sorted(regions),
            "events": [
                {
                    "event_arn": e.get("event_arn"),
                    "service": e.get("service"),
                    "event_type_code": e.get("event_type_code"),
                    "region": e.get("region"),
                    "status": e.get("status"),
                    "severity": e.get("severity"),
                    "org_id": e.get("org_id"),
                    "affected_account_count": e.get("affected_account_count", 0),
                }
                for e in events
            ],
        }, indent=2),
    ]

    return subject, "\n".join(lines)
