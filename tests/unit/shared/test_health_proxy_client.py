"""Unit tests for lambda/shared/health_proxy_client.py"""
import json
import pytest
from unittest.mock import patch, MagicMock

from health_proxy_client import HealthProxyClient, HealthAPIError, ThrottlingError


def _make_client(api_base_url="https://test.execute-api.us-east-1.amazonaws.com/prod"):
    with patch("health_proxy_client.boto3.Session") as mock_session, \
         patch("health_proxy_client.requests.Session"):
        mock_session.return_value.get_credentials.return_value = MagicMock()
        return HealthProxyClient(api_base_url=api_base_url)


def _mock_http_response(client, status_code: int, body: dict):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = json.dumps(body)
    mock_resp.json.return_value = body
    client.http.send = MagicMock(return_value=mock_resp)
    return mock_resp


# ── Constructor ───────────────────────────────────────────────────────────────

class TestInit:
    def test_strips_trailing_slash(self):
        client = _make_client("https://test.example.com/prod/")
        assert client.api_base_url == "https://test.example.com/prod"

    def test_sets_region(self):
        client = _make_client()
        assert client.region == "us-east-1"

    def test_custom_region(self):
        with patch("health_proxy_client.boto3.Session"), \
             patch("health_proxy_client.requests.Session"):
            client = HealthProxyClient("https://x.com/prod", region="eu-west-1")
        assert client.region == "eu-west-1"


# ── _signed_post ──────────────────────────────────────────────────────────────

class TestSignedPost:
    @patch("health_proxy_client.SigV4Auth")
    def test_returns_parsed_json_on_200(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {"events": [], "nextToken": None})
        result = client._signed_post("/describe-events-for-organization", {"filter": {}})
        assert result == {"events": [], "nextToken": None}

    @patch("health_proxy_client.SigV4Auth")
    def test_raises_throttling_on_429(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 429, {"message": "Too Many Requests"})
        with pytest.raises(ThrottlingError):
            client._signed_post("/test", {})

    @patch("health_proxy_client.SigV4Auth")
    def test_raises_throttling_on_400_throttling_exception(self, mock_auth):
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "ThrottlingException: Rate exceeded"
        client.http.send = MagicMock(return_value=mock_resp)
        with pytest.raises(ThrottlingError):
            client._signed_post("/test", {})

    @patch("health_proxy_client.SigV4Auth")
    def test_raises_health_api_error_on_other_error(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 500, {"message": "Internal Server Error"})
        with pytest.raises(HealthAPIError):
            client._signed_post("/test", {})

    @patch("health_proxy_client.SigV4Auth")
    def test_posts_json_body(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {})
        body = {"filter": {"eventTypeCategories": ["issue"]}}
        client._signed_post("/describe-events-for-organization", body)
        sent_request = client.http.send.call_args[0][0]
        assert json.loads(sent_request.body) == body


# ── _call (retry logic) ───────────────────────────────────────────────────────

class TestCall:
    @patch("health_proxy_client.SigV4Auth")
    @patch("health_proxy_client.time.sleep")
    def test_retries_on_throttling_then_succeeds(self, mock_sleep, mock_auth):
        client = _make_client()
        responses = [
            MagicMock(status_code=429, text="Throttled"),
            MagicMock(status_code=429, text="Throttled"),
            MagicMock(status_code=200, text=json.dumps({"ok": True}),
                      **{"json.return_value": {"ok": True}}),
        ]
        client.http.send = MagicMock(side_effect=responses)
        result = client._call("/test", {})
        assert result == {"ok": True}
        assert mock_sleep.call_count == 2

    @patch("health_proxy_client.SigV4Auth")
    @patch("health_proxy_client.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep, mock_auth):
        client = _make_client()
        client.http.send = MagicMock(
            return_value=MagicMock(status_code=429, text="Throttled")
        )
        with pytest.raises(ThrottlingError):
            client._call("/test", {})
        assert mock_sleep.call_count == client.MAX_RETRIES - 1

    @patch("health_proxy_client.SigV4Auth")
    def test_no_retry_on_success(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {"data": "ok"})
        result = client._call("/test", {})
        assert result == {"data": "ok"}
        assert client.http.send.call_count == 1


# ── describe_events_for_organization ─────────────────────────────────────────

class TestDescribeEvents:
    @patch("health_proxy_client.SigV4Auth")
    def test_returns_all_events_single_page(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {
            "events": [{"arn": "arn::1"}, {"arn": "arn::2"}]
        })
        result = client.describe_events_for_organization(["issue"], "2026-01-01T00:00:00Z")
        assert len(result) == 2

    @patch("health_proxy_client.SigV4Auth")
    def test_paginates_until_no_next_token(self, mock_auth):
        client = _make_client()
        responses = [
            MagicMock(status_code=200, text="",
                      **{"json.return_value": {"events": [{"arn": "1"}], "nextToken": "tok1"}}),
            MagicMock(status_code=200, text="",
                      **{"json.return_value": {"events": [{"arn": "2"}]}}),
        ]
        client.http.send = MagicMock(side_effect=responses)
        result = client.describe_events_for_organization(["issue"], "2026-01-01T00:00:00Z")
        assert len(result) == 2
        assert client.http.send.call_count == 2

    @patch("health_proxy_client.SigV4Auth")
    def test_returns_empty_on_no_events(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {"events": []})
        result = client.describe_events_for_organization(["issue"], "2026-01-01T00:00:00Z")
        assert result == []


# ── describe_affected_accounts_for_organization ───────────────────────────────

class TestDescribeAffectedAccounts:
    @patch("health_proxy_client.SigV4Auth")
    def test_returns_flat_account_list(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {"affectedAccounts": ["111", "222"]})
        result = client.describe_affected_accounts_for_organization("arn::event")
        assert result == ["111", "222"]

    @patch("health_proxy_client.SigV4Auth")
    def test_paginates_affected_accounts(self, mock_auth):
        client = _make_client()
        responses = [
            MagicMock(status_code=200, text="",
                      **{"json.return_value": {"affectedAccounts": ["111"], "nextToken": "t"}}),
            MagicMock(status_code=200, text="",
                      **{"json.return_value": {"affectedAccounts": ["222"]}}),
        ]
        client.http.send = MagicMock(side_effect=responses)
        result = client.describe_affected_accounts_for_organization("arn::event")
        assert result == ["111", "222"]


# ── describe_event_details_for_organization ───────────────────────────────────

class TestDescribeEventDetails:
    @patch("health_proxy_client.SigV4Auth")
    def test_single_batch_under_10(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {"successfulSet": [{"eventArn": "arn::1"}], "failedSet": []})
        result = client.describe_event_details_for_organization(["arn::1", "arn::2"])
        assert len(result["successfulSet"]) == 1
        assert client.http.send.call_count == 1

    @patch("health_proxy_client.SigV4Auth")
    def test_batches_at_10_arns(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {"successfulSet": [], "failedSet": []})
        arns = [f"arn::{i}" for i in range(25)]
        client.describe_event_details_for_organization(arns)
        # 25 ARNs → 3 batches (10+10+5)
        assert client.http.send.call_count == 3

    @patch("health_proxy_client.SigV4Auth")
    def test_includes_account_id_when_provided(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {"successfulSet": [], "failedSet": []})
        client.describe_event_details_for_organization(["arn::1"], account_id="123456789012")
        request_body = json.loads(client.http.send.call_args[0][0].body)
        filters = request_body["organizationEventDetailFilters"]
        assert filters[0]["awsAccountId"] == "123456789012"

    @patch("health_proxy_client.SigV4Auth")
    def test_merges_results_across_batches(self, mock_auth):
        client = _make_client()
        responses = [
            MagicMock(status_code=200, text="",
                      **{"json.return_value": {"successfulSet": [{"id": 1}], "failedSet": []}}),
            MagicMock(status_code=200, text="",
                      **{"json.return_value": {"successfulSet": [{"id": 2}], "failedSet": [{"id": 3}]}}),
        ]
        client.http.send = MagicMock(side_effect=responses)
        arns = [f"arn::{i}" for i in range(15)]
        result = client.describe_event_details_for_organization(arns)
        assert len(result["successfulSet"]) == 2
        assert len(result["failedSet"]) == 1


# ── describe_affected_entities_for_organization ───────────────────────────────

class TestDescribeAffectedEntities:
    @patch("health_proxy_client.SigV4Auth")
    def test_returns_entities(self, mock_auth):
        client = _make_client()
        _mock_http_response(client, 200, {
            "entities": [{"entityValue": "i-1234567890abcdef0"}]
        })
        result = client.describe_affected_entities_for_organization("arn::1", "111111111111")
        assert len(result) == 1
        assert result[0]["entityValue"] == "i-1234567890abcdef0"

    @patch("health_proxy_client.SigV4Auth")
    def test_paginates_entities(self, mock_auth):
        client = _make_client()
        responses = [
            MagicMock(status_code=200, text="",
                      **{"json.return_value": {"entities": [{"entityValue": "i-1"}], "nextToken": "t"}}),
            MagicMock(status_code=200, text="",
                      **{"json.return_value": {"entities": [{"entityValue": "i-2"}]}}),
        ]
        client.http.send = MagicMock(side_effect=responses)
        result = client.describe_affected_entities_for_organization("arn::1", "111111111111")
        assert len(result) == 2
