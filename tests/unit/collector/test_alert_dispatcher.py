"""Unit tests for lambda/collector/alert_dispatcher.py"""
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from botocore.exceptions import ClientError

import alert_dispatcher


def _make_incident(**kwargs):
    defaults = {
        "pk": "incident#EC2#2026010T1200",
        "service": "EC2",
        "regions": ["us-east-1"],
        "event_count": 2,
        "affected_account_count": 15,
        "severities": ["standard"],
        "event_type_codes": ["AWS_EC2_OPERATIONAL_ISSUE"],
        "org_ids": ["o-aaa"],
        "first_seen": "2026-01-01T12:00:00+00:00",
        "last_updated": "2026-01-01T12:15:00+00:00",
        "last_alerted_account_count": 0,
        "last_alerted_regions": [],
    }
    defaults.update(kwargs)
    return defaults


# ── _start_bucket ─────────────────────────────────────────────────────────────

class TestStartBucket:
    def test_returns_string(self):
        result = alert_dispatcher._start_bucket("2026-01-01T12:00:00Z")
        assert isinstance(result, str)

    def test_invalid_time_uses_now(self):
        # Should not raise, falls back to now
        result = alert_dispatcher._start_bucket("not-a-date")
        assert isinstance(result, str)
        assert len(result) == len("20260101T1200")  # YYYYMMDDTHHmm

    def test_same_window_same_bucket(self):
        t1 = "2026-01-01T12:05:00Z"
        t2 = "2026-01-01T12:45:00Z"
        assert alert_dispatcher._start_bucket(t1) == alert_dispatcher._start_bucket(t2)

    def test_different_window_different_bucket(self):
        t1 = "2026-01-01T12:05:00Z"
        t2 = "2026-01-01T14:05:00Z"
        assert alert_dispatcher._start_bucket(t1) != alert_dispatcher._start_bucket(t2)


# ── _new_incident / _merge_event / _to_dynamo ─────────────────────────────────

class TestIncidentHelpers:
    def test_new_incident_structure(self):
        inc = alert_dispatcher._new_incident("pk#svc#bucket", "EC2", "20260101T1200", "2026-01-01T12:00:00+00:00")
        assert inc["pk"] == "pk#svc#bucket"
        assert inc["service"] == "EC2"
        assert inc["event_arns"] == []
        assert inc["affected_account_count"] == 0

    def test_merge_event_adds_arn(self):
        inc = alert_dispatcher._new_incident("pk", "EC2", "bucket", "now")
        ev = {"event_arn": "arn::1", "region": "us-east-1", "org_id": "o-a",
              "severity": "critical", "event_type_code": "AWS_EC2_OUTAGE", "affected_account_count": 5}
        alert_dispatcher._merge_event(inc, ev)
        assert "arn::1" in inc["event_arns"]
        assert "us-east-1" in inc["regions"]
        assert inc["affected_account_count"] == 5

    def test_merge_event_deduplicates_arns(self):
        inc = alert_dispatcher._new_incident("pk", "EC2", "bucket", "now")
        ev = {"event_arn": "arn::1", "region": "us-east-1", "org_id": "o-a",
              "severity": "standard", "event_type_code": "CODE", "affected_account_count": 1}
        alert_dispatcher._merge_event(inc, ev)
        alert_dispatcher._merge_event(inc, ev)  # same event twice
        assert inc["event_arns"].count("arn::1") == 1

    def test_merge_event_takes_max_account_count(self):
        inc = alert_dispatcher._new_incident("pk", "EC2", "bucket", "now")
        inc["affected_account_count"] = 10
        ev = {"event_arn": "arn::2", "region": "eu-west-1", "org_id": "o-b",
              "severity": None, "event_type_code": None, "affected_account_count": 20}
        alert_dispatcher._merge_event(inc, ev)
        assert inc["affected_account_count"] == 20

    def test_to_dynamo_strips_none_and_empty(self):
        inc = {"pk": "pk", "service": "EC2", "regions": [], "empty_str": "", "none_val": None, "keep": "yes"}
        result = alert_dispatcher._to_dynamo(inc)
        assert "none_val" not in result
        assert "empty_str" not in result
        assert "regions" not in result
        assert result["keep"] == "yes"


# ── _has_significant_update ───────────────────────────────────────────────────

class TestHasSignificantUpdate:
    def test_account_count_doubled(self):
        inc = _make_incident(last_alerted_account_count=10, affected_account_count=20)
        assert alert_dispatcher._has_significant_update(inc) is True

    def test_account_count_not_quite_doubled(self):
        inc = _make_incident(last_alerted_account_count=10, affected_account_count=19,
                             last_alerted_regions=["us-east-1"], regions=["us-east-1"])
        assert alert_dispatcher._has_significant_update(inc) is False

    def test_new_region_triggers_update(self):
        inc = _make_incident(last_alerted_account_count=10, affected_account_count=10,
                             last_alerted_regions=["us-east-1"], regions=["us-east-1", "eu-west-1"])
        assert alert_dispatcher._has_significant_update(inc) is True

    def test_zero_last_count_skips_doubling_check(self):
        inc = _make_incident(last_alerted_account_count=0, affected_account_count=100,
                             last_alerted_regions=[], regions=[])
        # No new regions, last_count==0 → doubling check skipped → False
        assert alert_dispatcher._has_significant_update(inc) is False

    def test_no_change(self):
        inc = _make_incident(last_alerted_account_count=10, affected_account_count=15,
                             last_alerted_regions=["us-east-1"], regions=["us-east-1"])
        assert alert_dispatcher._has_significant_update(inc) is False


# ── _build_digest_message ─────────────────────────────────────────────────────

class TestBuildDigestMessage:
    def test_returns_three_tuple(self):
        result = alert_dispatcher._build_digest_message(_make_incident(), is_update=False)
        assert len(result) == 3

    def test_subject_max_100_chars(self):
        subject, _, _ = alert_dispatcher._build_digest_message(_make_incident(), is_update=False)
        assert len(subject) <= 100

    def test_new_incident_in_subject(self):
        subject, _, _ = alert_dispatcher._build_digest_message(_make_incident(), is_update=False)
        assert "NEW INCIDENT" in subject

    def test_update_in_subject_when_is_update(self):
        subject, _, attrs = alert_dispatcher._build_digest_message(_make_incident(), is_update=True)
        assert "UPDATE" in subject
        assert attrs["alert_type"]["StringValue"] == "UPDATE"

    def test_high_priority_multi_region_many_accounts(self):
        inc = _make_incident(regions=["us-east-1", "eu-west-1"], affected_account_count=200)
        _, _, attrs = alert_dispatcher._build_digest_message(inc, is_update=False)
        assert attrs["priority"]["StringValue"] == "HIGH"

    def test_standard_priority_single_region(self):
        _, _, attrs = alert_dispatcher._build_digest_message(_make_incident(), is_update=False)
        assert attrs["priority"]["StringValue"] == "STANDARD"

    def test_message_contains_health_aggregator_header(self):
        _, message, _ = alert_dispatcher._build_digest_message(_make_incident(), is_update=False)
        assert "AWS Health Aggregator" in message

    def test_message_contains_valid_json_payload(self):
        _, message, _ = alert_dispatcher._build_digest_message(_make_incident(), is_update=False)
        json_start = message.index("JSON payload:\n") + len("JSON payload:\n")
        payload = json.loads(message[json_start:])
        assert payload["service"] == "EC2"
        assert payload["event_count"] == 2

    def test_more_than_10_codes_shows_truncation(self):
        inc = _make_incident(event_type_codes=[f"CODE_{i}" for i in range(15)])
        _, message, _ = alert_dispatcher._build_digest_message(inc, is_update=False)
        assert "and 5 more" in message

    def test_exactly_10_codes_no_truncation(self):
        inc = _make_incident(event_type_codes=[f"CODE_{i}" for i in range(10)])
        _, message, _ = alert_dispatcher._build_digest_message(inc, is_update=False)
        assert "more" not in message

    def test_attributes_include_required_keys(self):
        _, _, attrs = alert_dispatcher._build_digest_message(_make_incident(), is_update=False)
        for key in ("priority", "service", "affected_accounts", "regions", "alert_type"):
            assert key in attrs


# ── _send_digest ──────────────────────────────────────────────────────────────

class TestSendDigest:
    @patch("alert_dispatcher._SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
    @patch("alert_dispatcher._sns")
    def test_publishes_and_returns_true(self, mock_sns):
        result = alert_dispatcher._send_digest(_make_incident())
        assert result is True
        mock_sns.publish.assert_called_once()

    @patch("alert_dispatcher._SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
    @patch("alert_dispatcher._sns")
    def test_publish_receives_correct_topic_arn(self, mock_sns):
        alert_dispatcher._send_digest(_make_incident())
        kwargs = mock_sns.publish.call_args.kwargs
        assert kwargs["TopicArn"] == "arn:aws:sns:us-east-1:123:topic"

    @patch("alert_dispatcher._SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
    @patch("alert_dispatcher._sns")
    def test_returns_false_on_client_error(self, mock_sns):
        mock_sns.publish.side_effect = ClientError(
            {"Error": {"Code": "InvalidParameter", "Message": "bad param"}}, "Publish"
        )
        result = alert_dispatcher._send_digest(_make_incident())
        assert result is False


# ── dispatch ──────────────────────────────────────────────────────────────────

class TestDispatch:
    @patch.object(alert_dispatcher, "_ALERTS_ENABLED", False)
    def test_disabled_returns_zero(self):
        assert alert_dispatcher.dispatch([], MagicMock()) == 0

    @patch.object(alert_dispatcher, "_ALERTS_ENABLED", True)
    @patch.object(alert_dispatcher, "_SNS_TOPIC_ARN", "")
    def test_no_topic_returns_zero(self):
        assert alert_dispatcher.dispatch([], MagicMock()) == 0

    @patch.object(alert_dispatcher, "_ALERTS_ENABLED", True)
    @patch.object(alert_dispatcher, "_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:t")
    @patch("alert_dispatcher._flush_digests", return_value=2)
    @patch("alert_dispatcher._correlate_events")
    def test_alertable_events_correlate_then_flush(self, mock_correlate, mock_flush):
        events = [{"status": "open", "is_operational": True, "service": "EC2"}]
        result = alert_dispatcher.dispatch(events, MagicMock())
        mock_correlate.assert_called_once()
        mock_flush.assert_called_once()
        assert result == 2

    @patch.object(alert_dispatcher, "_ALERTS_ENABLED", True)
    @patch.object(alert_dispatcher, "_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:t")
    @patch("alert_dispatcher._flush_digests", return_value=0)
    @patch("alert_dispatcher._correlate_events")
    def test_non_alertable_events_skip_correlate(self, mock_correlate, mock_flush):
        events = [
            {"status": "closed", "is_operational": True},
            {"status": "open", "is_operational": False},
        ]
        alert_dispatcher.dispatch(events, MagicMock())
        mock_correlate.assert_not_called()


# ── _mark_alerted ─────────────────────────────────────────────────────────────

class TestMarkAlerted:
    def test_updates_state_table(self):
        mock_table = MagicMock()
        inc = _make_incident(affected_account_count=10)
        alert_dispatcher._mark_alerted(inc, mock_table, "2026-01-01T12:00:00+00:00")
        mock_table.update_item.assert_called_once()
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"pk": inc["pk"]}

    def test_logs_warning_on_exception(self):
        mock_table = MagicMock()
        mock_table.update_item.side_effect = Exception("DDB error")
        # Should not raise
        alert_dispatcher._mark_alerted(_make_incident(), mock_table, "2026-01-01T00:00:00+00:00")


# ── _incident_pk ──────────────────────────────────────────────────────────────

class TestIncidentPk:
    def test_returns_formatted_string(self):
        pk = alert_dispatcher._incident_pk("EC2", "20260101T1200")
        assert pk == "incident#EC2#20260101T1200"


# ── _correlate_events ─────────────────────────────────────────────────────────

class TestCorrelateEvents:
    def _ev(self, arn="arn::1", service="EC2", start_time="2026-01-01T12:00:00+00:00"):
        return {
            "event_arn": arn,
            "service": service,
            "start_time": start_time,
            "region": "us-east-1",
            "org_id": "o-aaa",
            "severity": "standard",
            "event_type_code": "AWS_EC2_ISSUE",
            "affected_account_count": 5,
        }

    def test_creates_new_incident_on_ddb_miss(self):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no Item
        alert_dispatcher._correlate_events([self._ev()], mock_table)
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["pk"].startswith("incident#EC2#")

    def test_merges_events_into_existing_incident(self):
        existing = alert_dispatcher._new_incident("incident#EC2#20260101T1200", "EC2", "20260101T1200", "now")
        existing["event_arns"] = ["arn::0"]
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": existing}
        alert_dispatcher._correlate_events([self._ev("arn::1")], mock_table)
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "arn::0" in item.get("event_arns", [])

    def test_swallows_get_item_exception(self):
        mock_table = MagicMock()
        mock_table.get_item.side_effect = Exception("DDB error")
        # Should not raise; creates new incident and puts it
        alert_dispatcher._correlate_events([self._ev()], mock_table)
        mock_table.put_item.assert_called_once()

    def test_swallows_put_item_exception(self):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        mock_table.put_item.side_effect = Exception("DDB write error")
        # Should not raise
        alert_dispatcher._correlate_events([self._ev()], mock_table)

    def test_groups_same_service_same_bucket(self):
        events = [self._ev("arn::1"), self._ev("arn::2")]
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        alert_dispatcher._correlate_events(events, mock_table)
        # Both events same service+bucket → one put_item call
        assert mock_table.put_item.call_count == 1


# ── _flush_digests ────────────────────────────────────────────────────────────

class TestFlushDigests:
    def _mature_incident(self, **kwargs):
        """Incident that is old enough to trigger a first alert."""
        from datetime import timedelta, timezone
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        defaults = _make_incident(first_seen=old_time, last_alerted_account_count=0,
                                  last_alerted_regions=[])
        defaults.pop("alert_sent_at", None)  # no prior alert
        defaults.update(kwargs)
        return defaults

    def test_returns_zero_on_scan_error(self):
        mock_table = MagicMock()
        mock_table.scan.side_effect = Exception("DDB error")
        result = alert_dispatcher._flush_digests(mock_table)
        assert result == 0

    def test_sends_digest_for_mature_incident(self):
        inc = self._mature_incident()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [inc]}
        with patch("alert_dispatcher._send_digest", return_value=True) as mock_send, \
             patch("alert_dispatcher._mark_alerted"):
            result = alert_dispatcher._flush_digests(mock_table)
        mock_send.assert_called_once()
        assert result == 1

    def test_skips_immature_incident(self):
        from datetime import timedelta, timezone
        recent_time = datetime.now(timezone.utc).isoformat()
        inc = _make_incident(first_seen=recent_time)
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [inc]}
        with patch("alert_dispatcher._send_digest") as mock_send:
            result = alert_dispatcher._flush_digests(mock_table)
        mock_send.assert_not_called()
        assert result == 0

    def test_sends_update_for_significant_change(self):
        inc = self._mature_incident(
            alert_sent_at="2026-01-01T10:00:00+00:00",
            last_alerted_account_count=5,
            affected_account_count=20,  # doubled → significant
            last_alerted_regions=["us-east-1"],
            regions=["us-east-1"],
        )
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [inc]}
        with patch("alert_dispatcher._send_digest", return_value=True) as mock_send, \
             patch("alert_dispatcher._mark_alerted"):
            result = alert_dispatcher._flush_digests(mock_table)
        mock_send.assert_called_once()
        assert result == 1

    def test_skips_insignificant_update(self):
        inc = self._mature_incident(
            alert_sent_at="2026-01-01T10:00:00+00:00",
            last_alerted_account_count=10,
            affected_account_count=11,  # not doubled
            last_alerted_regions=["us-east-1"],
            regions=["us-east-1"],
        )
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [inc]}
        with patch("alert_dispatcher._send_digest") as mock_send:
            alert_dispatcher._flush_digests(mock_table)
        mock_send.assert_not_called()

    def test_marks_alerted_after_send(self):
        inc = self._mature_incident()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [inc]}
        with patch("alert_dispatcher._send_digest", return_value=True), \
             patch("alert_dispatcher._mark_alerted") as mock_mark:
            alert_dispatcher._flush_digests(mock_table)
        mock_mark.assert_called_once()
