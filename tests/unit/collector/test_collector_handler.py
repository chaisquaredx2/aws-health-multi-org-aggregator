"""Unit tests for lambda/collector/handler.py"""
import sys
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import collector_handler as ch


def _org(org_id="o-aaa", with_external_id=False):
    o = {
        "org_id": org_id,
        "org_name": f"Org {org_id}",
        "assume_role_arn": f"arn:aws:iam::123456789012:role/health-aggregator-{org_id}",
    }
    if with_external_id:
        o["assume_role_external_id"] = "ext-secret"
    return o


# ── _iso ──────────────────────────────────────────────────────────────────────

class TestIso:
    def test_none_returns_empty_string(self):
        assert ch._iso(None) == ""

    def test_string_passthrough(self):
        s = "2026-01-01T12:00:00Z"
        assert ch._iso(s) == s

    def test_datetime_with_tz(self):
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = ch._iso(dt)
        assert "2026-01-01" in result
        assert "12:00:00" in result

    def test_datetime_without_tz_adds_utc(self):
        dt = datetime(2026, 3, 15, 0, 0, 0)
        result = ch._iso(dt)
        assert "+00:00" in result


# ── _assume_org_role ──────────────────────────────────────────────────────────

class TestAssumeOrgRole:
    def test_returns_credentials(self):
        creds = {"AccessKeyId": "AKID", "SecretAccessKey": "SEC", "SessionToken": "TOK"}
        with patch.object(ch, "_sts") as mock_sts:
            mock_sts.assume_role.return_value = {"Credentials": creds}
            result = ch._assume_org_role(_org())
        assert result == creds

    def test_passes_role_arn(self):
        org = _org()
        with patch.object(ch, "_sts") as mock_sts:
            mock_sts.assume_role.return_value = {"Credentials": {}}
            ch._assume_org_role(org)
        kwargs = mock_sts.assume_role.call_args.kwargs
        assert kwargs["RoleArn"] == org["assume_role_arn"]

    def test_includes_external_id_when_set(self):
        org = _org(with_external_id=True)
        with patch.object(ch, "_sts") as mock_sts:
            mock_sts.assume_role.return_value = {"Credentials": {}}
            ch._assume_org_role(org)
        kwargs = mock_sts.assume_role.call_args.kwargs
        assert kwargs["ExternalId"] == "ext-secret"

    def test_omits_external_id_when_not_set(self):
        org = _org()
        with patch.object(ch, "_sts") as mock_sts:
            mock_sts.assume_role.return_value = {"Credentials": {}}
            ch._assume_org_role(org)
        kwargs = mock_sts.assume_role.call_args.kwargs
        assert "ExternalId" not in kwargs


# ── _emit_metric ──────────────────────────────────────────────────────────────

class TestEmitMetric:
    def test_calls_cloudwatch(self):
        with patch.object(ch, "_cloudwatch") as mock_cw:
            ch._emit_metric("EventsCollected", 42)
        mock_cw.put_metric_data.assert_called_once()

    def test_unit_is_count_for_events(self):
        with patch.object(ch, "_cloudwatch") as mock_cw:
            ch._emit_metric("EventsCollected", 5)
        data = mock_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert data["Unit"] == "Count"

    def test_unit_is_milliseconds_for_duration(self):
        with patch.object(ch, "_cloudwatch") as mock_cw:
            ch._emit_metric("OrgCollectionDurationMs", 1234, "o-aaa")
        data = mock_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert data["Unit"] == "Milliseconds"

    def test_swallows_exception(self):
        with patch.object(ch, "_cloudwatch") as mock_cw:
            mock_cw.put_metric_data.side_effect = Exception("CW error")
            ch._emit_metric("EventsCollected", 1)  # must not raise

    def test_org_id_default_is_all(self):
        with patch.object(ch, "_cloudwatch") as mock_cw:
            ch._emit_metric("EventsCollected", 0)
        dims = mock_cw.put_metric_data.call_args.kwargs["MetricData"][0]["Dimensions"]
        assert dims[0]["Value"] == "all"


# ── _put_collection_state ─────────────────────────────────────────────────────

class TestPutCollectionState:
    def test_success_writes_last_successful_at(self):
        with patch.object(ch, "_state_table") as mock_table:
            ch._put_collection_state(_org(), success=True, error=None, count=10)
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "last_successful_at" in item
        assert item["events_in_window"] == 10

    def test_failure_writes_last_error(self):
        with patch.object(ch, "_state_table") as mock_table:
            ch._put_collection_state(_org(), success=False, error="boom", count=0)
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["last_error"] == "boom"
        assert "last_successful_at" not in item

    def test_failure_uses_unknown_when_error_is_none(self):
        with patch.object(ch, "_state_table") as mock_table:
            ch._put_collection_state(_org(), success=False, error=None, count=0)
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["last_error"] == "unknown"

    def test_pk_is_org_id(self):
        org = _org("o-test")
        with patch.object(ch, "_state_table") as mock_table:
            ch._put_collection_state(org, success=True, error=None, count=0)
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["pk"] == "o-test"


# ── _process_event ────────────────────────────────────────────────────────────

class TestProcessEvent:
    def _ev(self):
        return {
            "arn": "arn:aws:health:us-east-1::event/EC2/AWS_EC2_ISSUE/evt1",
            "eventTypeCategory": "issue",
            "startTime": "2026-01-01T12:00:00Z",
            "service": "EC2",
            "eventTypeCode": "AWS_EC2_OPERATIONAL_ISSUE",
            "statusCode": "open",
            "region": "us-east-1",
        }

    def test_returns_zero_when_no_org_accounts(self):
        ev = self._ev()
        mock_client = MagicMock()
        mock_client.describe_affected_accounts_for_organization.return_value = ["999"]
        count, item = ch._process_event(ev, "o-aaa", "Org A", {}, mock_client)
        assert count == 0
        assert item is None

    def test_returns_one_and_item_when_accounts_match(self):
        ev = self._ev()
        mock_client = MagicMock()
        mock_client.describe_affected_accounts_for_organization.return_value = ["111"]
        account_map = {"111": {"account_name": "A", "business_unit": "Eng", "environment": "production"}}
        with patch.object(ch, "_events_table") as mock_table, \
             patch("collector_handler.classify_event") as mock_classify:
            mock_classify.return_value = MagicMock(is_operational=True, severity="standard")
            count, item = ch._process_event(ev, "o-aaa", "Org A", account_map, mock_client)
        assert count == 1
        assert item is not None
        mock_table.put_item.assert_called_once()

    def test_item_pk_contains_arn_and_org_id(self):
        ev = self._ev()
        mock_client = MagicMock()
        mock_client.describe_affected_accounts_for_organization.return_value = ["111"]
        account_map = {"111": {"account_name": "A", "business_unit": "Eng", "environment": "production"}}
        with patch.object(ch, "_events_table"), \
             patch("collector_handler.classify_event") as mock_classify:
            mock_classify.return_value = MagicMock(is_operational=True, severity="standard")
            _, item = ch._process_event(ev, "o-aaa", "Org A", account_map, mock_client)
        assert item["pk"] == f"{ev['arn']}#o-aaa"

    def test_none_values_stripped(self):
        ev = self._ev()  # no endTime → None
        mock_client = MagicMock()
        mock_client.describe_affected_accounts_for_organization.return_value = ["111"]
        account_map = {"111": {"account_name": "A", "business_unit": "Eng", "environment": "production"}}
        with patch.object(ch, "_events_table"), \
             patch("collector_handler.classify_event") as mock_classify:
            mock_classify.return_value = MagicMock(is_operational=True, severity="standard")
            _, item = ch._process_event(ev, "o-aaa", "Org A", account_map, mock_client)
        assert "end_time" not in item

    def test_filters_accounts_not_in_org(self):
        ev = self._ev()
        mock_client = MagicMock()
        mock_client.describe_affected_accounts_for_organization.return_value = ["111", "222"]
        account_map = {"111": {"account_name": "A", "business_unit": "Eng", "environment": "production"}}
        with patch.object(ch, "_events_table"), \
             patch("collector_handler.classify_event") as mock_classify:
            mock_classify.return_value = MagicMock(is_operational=True, severity="standard")
            _, item = ch._process_event(ev, "o-aaa", "Org A", account_map, mock_client)
        assert item["affected_account_count"] == 1


# ── handler ───────────────────────────────────────────────────────────────────

class TestHandler:
    def test_returns_200_on_success(self):
        with patch("collector_handler.load_orgs", return_value=[_org()]), \
             patch("collector_handler._collect_org", return_value=(3, [])), \
             patch("collector_handler.dispatch_alerts", return_value=0), \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler.HealthProxyClient"):
            result = ch.handler({}, None)
        assert result["statusCode"] == 200
        assert result["total_events"] == 3

    def test_returns_200_with_no_orgs(self):
        with patch("collector_handler.load_orgs", return_value=[]), \
             patch("collector_handler.dispatch_alerts", return_value=0), \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler.HealthProxyClient"):
            result = ch.handler({}, None)
        assert result["statusCode"] == 200
        assert result["total_events"] == 0

    def test_returns_207_on_org_error(self):
        def _fail(*args):
            raise RuntimeError("collect failed")
        with patch("collector_handler.load_orgs", return_value=[_org()]), \
             patch("collector_handler._collect_org", side_effect=_fail), \
             patch("collector_handler.dispatch_alerts", return_value=0), \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler._put_collection_state"), \
             patch("collector_handler.HealthProxyClient"):
            result = ch.handler({}, None)
        assert result["statusCode"] == 207
        assert len(result["errors"]) == 1

    def test_error_entry_contains_org_id(self):
        def _fail(*args):
            raise RuntimeError("boom")
        with patch("collector_handler.load_orgs", return_value=[_org("o-fail")]), \
             patch("collector_handler._collect_org", side_effect=_fail), \
             patch("collector_handler.dispatch_alerts", return_value=0), \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler._put_collection_state"), \
             patch("collector_handler.HealthProxyClient"):
            result = ch.handler({}, None)
        assert result["errors"][0]["org_id"] == "o-fail"

    def test_dispatches_alerts_after_collection(self):
        mock_dispatch = MagicMock(return_value=2)
        with patch("collector_handler.load_orgs", return_value=[_org()]), \
             patch("collector_handler._collect_org", return_value=(1, [{"pk": "x"}])), \
             patch("collector_handler.dispatch_alerts", mock_dispatch), \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler.HealthProxyClient"):
            ch.handler({}, None)
        mock_dispatch.assert_called_once()

    def test_multiple_orgs_aggregate_counts(self):
        with patch("collector_handler.load_orgs", return_value=[_org("o-1"), _org("o-2")]), \
             patch("collector_handler._collect_org", return_value=(5, [])), \
             patch("collector_handler.dispatch_alerts", return_value=0), \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler.HealthProxyClient"):
            result = ch.handler({}, None)
        assert result["total_events"] == 10


# ── _collect_org ──────────────────────────────────────────────────────────────

class TestCollectOrg:
    def _ev(self):
        return {
            "arn": "arn:aws:health:us-east-1::event/EC2/AWS_EC2_ISSUE/evt1",
            "eventTypeCategory": "issue",
            "startTime": "2026-01-01T12:00:00Z",
            "service": "EC2",
            "eventTypeCode": "AWS_EC2_OPERATIONAL_ISSUE",
            "statusCode": "open",
            "region": "us-east-1",
        }

    def test_returns_count_and_items(self):
        org = _org()
        mock_health = MagicMock()
        mock_health.describe_events_for_organization.return_value = [self._ev()]
        account_map = {"111": {"account_name": "A", "business_unit": "Eng", "environment": "production"}}
        mock_health.describe_affected_accounts_for_organization.return_value = ["111"]

        with patch("collector_handler._assume_org_role", return_value={}), \
             patch("collector_handler.load_account_map", return_value=account_map), \
             patch.object(ch, "_events_table"), \
             patch("collector_handler.classify_event") as mock_cls, \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler._put_collection_state"):
            mock_cls.return_value = MagicMock(is_operational=True, severity="standard")
            count, items = ch._collect_org(org, mock_health, "2026-01-01T00:00:00Z")
        assert count == 1
        assert len(items) == 1

    def test_skips_event_on_process_exception(self):
        org = _org()
        mock_health = MagicMock()
        mock_health.describe_events_for_organization.return_value = [self._ev()]

        with patch("collector_handler._assume_org_role", return_value={}), \
             patch("collector_handler.load_account_map", return_value={}), \
             patch("collector_handler._process_event", side_effect=Exception("oops")), \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler._put_collection_state"):
            count, items = ch._collect_org(org, mock_health, "2026-01-01T00:00:00Z")
        assert count == 0
        assert items == []

    def test_writes_collection_state_on_success(self):
        org = _org()
        mock_health = MagicMock()
        mock_health.describe_events_for_organization.return_value = []

        with patch("collector_handler._assume_org_role", return_value={}), \
             patch("collector_handler.load_account_map", return_value={}), \
             patch("collector_handler._emit_metric"), \
             patch("collector_handler._put_collection_state") as mock_state:
            ch._collect_org(org, mock_health, "2026-01-01T00:00:00Z")
        mock_state.assert_called_once()
        kwargs = mock_state.call_args.kwargs
        assert kwargs["success"] is True
