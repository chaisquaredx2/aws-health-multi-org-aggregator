"""Unit tests for lambda/api/routes/events.py"""
import base64
import json
import pytest
from unittest.mock import patch, MagicMock

from events import list_events, get_event_details, _merge_by_arn, _window_bounds


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _item(arn="arn::1", org_id="o-aaa", service="EC2", region="us-east-1",
          status="open", category="issue", start_time="2026-01-01T12:00:00+00:00",
          accounts=None):
    return {
        "pk": f"{arn}#{org_id}",
        "event_arn": arn,
        "org_id": org_id,
        "org_name": "Org A",
        "category": category,
        "service": service,
        "event_type_code": "AWS_EC2_ISSUE",
        "region": region,
        "status": status,
        "start_time": start_time,
        "last_updated_time": "2026-01-01T13:00:00+00:00",
        "affected_accounts": accounts or [{"account_id": "111", "environment": "production"}],
        "affected_account_count": 1,
    }


def _mock_table(items, lek=None):
    resp = {"Items": items}
    if lek:
        resp["LastEvaluatedKey"] = lek
    mock = MagicMock()
    mock.query.return_value = resp
    return mock


# ── _window_bounds ────────────────────────────────────────────────────────────

class TestWindowBounds:
    def test_returns_two_datetimes(self):
        start, end = _window_bounds(7)
        assert start < end

    def test_difference_equals_window_days(self):
        start, end = _window_bounds(3)
        assert (end - start).days == 3

    def test_end_is_after_start(self):
        start, end = _window_bounds(1)
        assert end > start


# ── _merge_by_arn ─────────────────────────────────────────────────────────────

class TestMergeByArn:
    def test_single_item(self):
        merged = _merge_by_arn([_item()])
        assert len(merged) == 1
        assert merged[0]["event_arn"] == "arn::1"

    def test_two_orgs_same_arn_merged(self):
        items = [_item("arn::1", "o-aaa"), _item("arn::1", "o-bbb")]
        merged = _merge_by_arn(items)
        assert len(merged) == 1
        assert len(merged[0]["affected_orgs"]) == 2
        assert merged[0]["affected_account_count"] == 2

    def test_two_different_arns(self):
        items = [_item("arn::1"), _item("arn::2")]
        merged = _merge_by_arn(items)
        assert len(merged) == 2

    def test_latest_updated_metadata_wins(self):
        old = _item("arn::1", "o-aaa")
        old["last_updated_time"] = "2026-01-01T10:00:00+00:00"
        old["status"] = "open"
        new = _item("arn::1", "o-bbb")
        new["last_updated_time"] = "2026-01-01T14:00:00+00:00"
        new["status"] = "closed"
        merged = _merge_by_arn([old, new])
        assert merged[0]["status"] == "closed"

    def test_empty_list(self):
        assert _merge_by_arn([]) == []

    def test_affected_account_count_sums_across_orgs(self):
        item1 = _item("arn::1", "o-aaa", accounts=[{"account_id": "1"}, {"account_id": "2"}])
        item2 = _item("arn::1", "o-bbb", accounts=[{"account_id": "3"}])
        merged = _merge_by_arn([item1, item2])
        assert merged[0]["affected_account_count"] == 3


# ── list_events ───────────────────────────────────────────────────────────────

class TestListEvents:
    def test_invalid_category_raises_value_error(self):
        with pytest.raises(ValueError, match="category"):
            list_events({"category": "invalid"}, {})

    def test_missing_category_raises_value_error(self):
        with pytest.raises(ValueError, match="category"):
            list_events({}, {})

    def test_window_days_too_large_raises_value_error(self):
        with pytest.raises(ValueError, match="window_days"):
            list_events({"category": "issue", "window_days": "999"}, {})

    def test_window_days_zero_raises_value_error(self):
        with pytest.raises(ValueError, match="window_days"):
            list_events({"category": "issue", "window_days": "0"}, {})

    def test_invalid_status_raises_value_error(self):
        with pytest.raises(ValueError, match="status"):
            list_events({"category": "issue", "status": "unknown"}, {})

    def test_returns_events(self):
        with patch("events._table", _mock_table([_item()])):
            result = list_events({"category": "issue"}, {})
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["meta"]["total"] == 1
        assert len(body["data"]) == 1

    def test_service_filter_case_insensitive(self):
        items = [_item(service="EC2"), _item("arn::2", service="RDS")]
        with patch("events._table", _mock_table(items)):
            result = list_events({"category": "issue", "service": "ec2"}, {})
        body = json.loads(result["body"])
        assert body["meta"]["total"] == 1

    def test_region_filter(self):
        items = [_item(region="us-east-1"), _item("arn::2", region="eu-west-1")]
        with patch("events._table", _mock_table(items)):
            result = list_events({"category": "issue", "region": "us-east-1"}, {})
        body = json.loads(result["body"])
        assert body["meta"]["total"] == 1

    def test_status_filter_via_query(self):
        items = [_item(status="open"), _item("arn::2", status="closed")]
        with patch("events._table", _mock_table(items)):
            result = list_events({"category": "issue", "status": "open"}, {})
        body = json.loads(result["body"])
        assert body["meta"]["total"] == 1

    def test_status_filter_via_multi_query(self):
        items = [_item(status="open"), _item("arn::2", status="closed"), _item("arn::3", status="upcoming")]
        with patch("events._table", _mock_table(items)):
            result = list_events({"category": "issue"}, {"status": ["open", "closed"]})
        body = json.loads(result["body"])
        assert body["meta"]["total"] == 2

    def test_org_id_filter(self):
        items = [_item(org_id="o-aaa"), _item("arn::2", org_id="o-bbb")]
        with patch("events._table", _mock_table(items)):
            result = list_events({"category": "issue", "org_id": "o-aaa"}, {})
        body = json.loads(result["body"])
        assert body["meta"]["total"] == 1

    def test_environment_filter(self):
        item1 = _item(accounts=[{"account_id": "1", "environment": "production"}])
        item2 = _item("arn::2", accounts=[{"account_id": "2", "environment": "non-production"}])
        with patch("events._table", _mock_table([item1, item2])):
            result = list_events({"category": "issue", "environment": "production"}, {})
        body = json.loads(result["body"])
        assert body["meta"]["total"] == 1

    def test_next_token_present_when_lek(self):
        lek = {"pk": "arn::1#o-aaa", "sk": "issue#2026"}
        with patch("events._table", _mock_table([_item()], lek=lek)):
            result = list_events({"category": "issue"}, {})
        body = json.loads(result["body"])
        assert body["meta"]["next_token"] is not None

    def test_no_next_token_when_no_lek(self):
        with patch("events._table", _mock_table([_item()])):
            result = list_events({"category": "issue"}, {})
        body = json.loads(result["body"])
        assert body["meta"]["next_token"] is None

    def test_page_size_capped_at_200(self):
        with patch("events._table") as mock_table:
            mock_table.query.return_value = {"Items": []}
            list_events({"category": "issue", "page_size": "500"}, {})
        kwargs = mock_table.query.call_args.kwargs
        assert kwargs["Limit"] == 200

    def test_page_size_default_100(self):
        with patch("events._table") as mock_table:
            mock_table.query.return_value = {"Items": []}
            list_events({"category": "issue"}, {})
        kwargs = mock_table.query.call_args.kwargs
        assert kwargs["Limit"] == 100

    def test_next_token_decoded_as_start_key(self):
        import base64, json as _json
        token = base64.b64encode(_json.dumps({"pk": "x"}).encode()).decode()
        with patch("events._table") as mock_table:
            mock_table.query.return_value = {"Items": []}
            list_events({"category": "issue", "next_token": token}, {})
        kwargs = mock_table.query.call_args.kwargs
        assert "ExclusiveStartKey" in kwargs


# ── get_event_details ─────────────────────────────────────────────────────────

class TestGetEventDetails:
    def test_invalid_base64_raises_value_error(self):
        with pytest.raises(ValueError, match="base64"):
            get_event_details({}, {}, "!!!not-valid!!!")

    def test_not_found_returns_404(self):
        with patch("events._table") as mock_table:
            mock_table.scan.return_value = {"Items": []}
            result = get_event_details({}, {}, _b64("arn::1"))
        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"]["code"] == "NOT_FOUND"

    def test_found_returns_200(self):
        with patch("events._table") as mock_table, \
             patch("events._HEALTH_PROXY_URL", ""):
            mock_table.scan.return_value = {"Items": [_item()]}
            result = get_event_details({}, {}, _b64("arn::1"))
        assert result["statusCode"] == 200

    def test_org_filter_no_match_returns_404(self):
        with patch("events._table") as mock_table, \
             patch("events._HEALTH_PROXY_URL", ""):
            mock_table.scan.return_value = {"Items": [_item(org_id="o-aaa")]}
            result = get_event_details({"org_id": "o-bbb"}, {}, _b64("arn::1"))
        assert result["statusCode"] == 404

    def test_fetches_description_when_proxy_url_set(self):
        item = _item()
        with patch("events._table") as mock_table, \
             patch("events._HEALTH_PROXY_URL", "https://test.api.aws/prod"), \
             patch("events._fetch_description", return_value={"latest_description": "desc"}) as mock_desc:
            mock_table.scan.return_value = {"Items": [item]}
            result = get_event_details({}, {}, _b64("arn::1"))
        mock_desc.assert_called_once_with("arn::1", "o-aaa")
        body = json.loads(result["body"])
        assert body["description"]["latest_description"] == "desc"

    def test_no_description_fetch_without_proxy_url(self):
        with patch("events._table") as mock_table, \
             patch("events._HEALTH_PROXY_URL", ""), \
             patch("events._fetch_description") as mock_desc:
            mock_table.scan.return_value = {"Items": [_item()]}
            get_event_details({}, {}, _b64("arn::1"))
        mock_desc.assert_not_called()

    def test_merged_result_has_affected_orgs(self):
        items = [_item("arn::1", "o-aaa"), _item("arn::1", "o-bbb")]
        with patch("events._table") as mock_table, \
             patch("events._HEALTH_PROXY_URL", ""):
            mock_table.scan.return_value = {"Items": items}
            result = get_event_details({}, {}, _b64("arn::1"))
        body = json.loads(result["body"])
        assert len(body["affected_orgs"]) == 2


# ── _fetch_description ────────────────────────────────────────────────────────

class TestFetchDescription:
    # _fetch_description does a deferred `from health_proxy_client import HealthProxyClient`
    # inside the function body, so we patch the source module, not events.HealthProxyClient.

    def test_returns_description_on_success(self):
        from events import _fetch_description
        mock_client = MagicMock()
        mock_client.describe_event_details_for_organization.return_value = {
            "successfulSet": [{
                "eventDescription": {"latestDescription": "Service disruption"},
                "event": {"lastUpdatedTime": "2026-01-01T13:00:00Z"},
            }]
        }
        with patch("health_proxy_client.HealthProxyClient", return_value=mock_client):
            result = _fetch_description("arn::1", "o-aaa")
        assert result["latest_description"] == "Service disruption"
        assert result["fetched_from_org_id"] == "o-aaa"

    def test_returns_empty_dict_on_exception(self):
        from events import _fetch_description

        def _raise(*args, **kwargs):
            raise Exception("connection error")

        with patch("health_proxy_client.HealthProxyClient", side_effect=_raise):
            result = _fetch_description("arn::1", "o-aaa")
        assert result == {}

    def test_returns_empty_when_no_description(self):
        from events import _fetch_description
        mock_client = MagicMock()
        mock_client.describe_event_details_for_organization.return_value = {
            "successfulSet": [{"eventDescription": {}}]
        }
        with patch("health_proxy_client.HealthProxyClient", return_value=mock_client):
            result = _fetch_description("arn::1", "o-aaa")
        assert result == {}
