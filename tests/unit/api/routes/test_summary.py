"""Unit tests for lambda/api/routes/summary.py"""
import json
import pytest
from unittest.mock import patch, MagicMock

from summary import get_summary


def _item(org_id="o-aaa", category="issue", status="open",
          service="EC2", region="us-east-1", accounts=None):
    return {
        "pk": f"arn::1#{org_id}",
        "event_arn": "arn::1",
        "org_id": org_id,
        "org_name": f"Org {org_id}",
        "category": category,
        "service": service,
        "region": region,
        "status": status,
        "start_time": "2026-01-01T12:00:00+00:00",
        "affected_accounts": accounts or [{"account_id": "111"}],
    }


def _mock_table(items):
    mock = MagicMock()
    mock.query.return_value = {"Items": items}
    return mock


# ── get_summary ───────────────────────────────────────────────────────────────

class TestGetSummary:
    def test_returns_200(self):
        with patch("summary._table", _mock_table([])):
            result = get_summary({}, {})
        assert result["statusCode"] == 200

    def test_empty_dynamo_returns_zero_counts(self):
        with patch("summary._table", _mock_table([])):
            result = get_summary({}, {})
        body = json.loads(result["body"])
        assert body["summary"]["affected_account_count"] == 0
        assert body["summary"]["issues"] == {}
        assert body["summary"]["investigations"] == {}

    def test_counts_issue_totals(self):
        items = [_item(status="open"), _item("o-bbb", status="closed")]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({"category": "issue"}, {})
        body = json.loads(result["body"])
        assert body["summary"]["issues"]["total"] == 2
        assert body["summary"]["issues"]["open"] == 1
        assert body["summary"]["issues"]["closed"] == 1

    def test_counts_investigations_separately(self):
        items = [_item(category="investigation")]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({"category": "investigation"}, {})
        body = json.loads(result["body"])
        assert body["summary"]["investigations"]["total"] == 1

    def test_category_all_queries_both_categories(self):
        mock = MagicMock()
        mock.query.return_value = {"Items": []}
        with patch("summary._table", mock):
            get_summary({"category": "all"}, {})
        assert mock.query.call_count == 2

    def test_specific_category_queries_once(self):
        mock = MagicMock()
        mock.query.return_value = {"Items": []}
        with patch("summary._table", mock):
            get_summary({"category": "issue"}, {})
        assert mock.query.call_count == 1

    def test_org_id_filter(self):
        items = [_item("o-aaa"), _item("o-bbb")]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({"org_id": "o-aaa"}, {})
        body = json.loads(result["body"])
        assert len(body["summary"]["by_org"]) == 1
        assert body["summary"]["by_org"][0]["org_id"] == "o-aaa"

    def test_by_org_breakdown_contains_all_orgs(self):
        items = [_item("o-aaa"), _item("o-bbb")]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({}, {})
        body = json.loads(result["body"])
        org_ids = {o["org_id"] for o in body["summary"]["by_org"]}
        assert "o-aaa" in org_ids
        assert "o-bbb" in org_ids

    def test_by_org_contains_issue_counts(self):
        items = [_item("o-aaa", status="open"), _item("o-aaa", status="closed")]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({"category": "issue"}, {})
        body = json.loads(result["body"])
        org = body["summary"]["by_org"][0]
        assert org["issues"]["open"] == 1
        assert org["issues"]["closed"] == 1

    def test_top_affected_services(self):
        # Use explicit category to avoid double-counting from the "all" default
        items = [_item(service="EC2"), _item("o-b", service="EC2"), _item("o-c", service="RDS")]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({"category": "issue"}, {})
        body = json.loads(result["body"])
        services = {s["service"]: s["event_count"] for s in body["summary"]["top_affected_services"]}
        assert services["EC2"] == 2
        assert services["RDS"] == 1

    def test_top_affected_regions(self):
        items = [_item(region="us-east-1"), _item("o-b", region="eu-west-1")]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({}, {})
        body = json.loads(result["body"])
        regions = {r["region"] for r in body["summary"]["top_affected_regions"]}
        assert "us-east-1" in regions
        assert "eu-west-1" in regions

    def test_affected_account_count_deduplicates(self):
        # Same account_id across two events → only counted once
        items = [
            _item(accounts=[{"account_id": "111"}]),
            _item("o-bbb", accounts=[{"account_id": "111"}]),
        ]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({}, {})
        body = json.loads(result["body"])
        assert body["summary"]["affected_account_count"] == 1

    def test_affected_account_count_across_multiple_accounts(self):
        items = [
            _item(accounts=[{"account_id": "111"}, {"account_id": "222"}]),
            _item("o-bbb", accounts=[{"account_id": "333"}]),
        ]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({}, {})
        body = json.loads(result["body"])
        assert body["summary"]["affected_account_count"] == 3

    def test_meta_includes_window_info(self):
        with patch("summary._table", _mock_table([])):
            result = get_summary({}, {})
        body = json.loads(result["body"])
        assert "window_start" in body["meta"]
        assert "window_end" in body["meta"]
        assert "window_days" in body["meta"]

    def test_paginated_dynamo_followed(self):
        """get_summary must follow LastEvaluatedKey pages."""
        item = _item()
        mock = MagicMock()
        mock.query.side_effect = [
            {"Items": [item], "LastEvaluatedKey": {"pk": "x"}},
            {"Items": [item]},
        ]
        with patch("summary._table", mock):
            result = get_summary({"category": "issue"}, {})
        body = json.loads(result["body"])
        assert body["summary"]["issues"]["total"] == 2

    def test_top_services_max_10(self):
        items = [_item(service=f"SVC{i}") for i in range(15)]
        with patch("summary._table", _mock_table(items)):
            result = get_summary({}, {})
        body = json.loads(result["body"])
        assert len(body["summary"]["top_affected_services"]) <= 10
