"""Unit tests for lambda/api/handler.py"""
import json
import pytest
from unittest.mock import patch, MagicMock

import api_handler as ah


def _apigw(method="GET", path="/v1/events", query=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": query or {},
        "multiValueQueryStringParameters": {},
    }


# ── OPTIONS preflight ─────────────────────────────────────────────────────────

class TestOptions:
    def test_options_returns_200(self):
        result = ah.handler(_apigw("OPTIONS", "/v1/events"), None)
        assert result["statusCode"] == 200

    def test_options_returns_cors_headers(self):
        result = ah.handler(_apigw("OPTIONS", "/v1/events"), None)
        assert "Access-Control-Allow-Origin" in result["headers"]
        assert "Access-Control-Allow-Methods" in result["headers"]
        assert "Access-Control-Allow-Headers" in result["headers"]

    def test_options_on_any_path(self):
        result = ah.handler(_apigw("OPTIONS", "/v1/summary"), None)
        assert result["statusCode"] == 200


# ── Route dispatch ────────────────────────────────────────────────────────────
# _ROUTES captures function references at import time, so we must patch the
# _ROUTES list itself (not the module attribute) to intercept dispatch.

def _patched_routes(fn_name, mock_fn):
    """Return _ROUTES with the named function replaced by mock_fn."""
    return [
        (method, pattern, mock_fn if fn.__name__ == fn_name else fn, hp)
        for method, pattern, fn, hp in ah._ROUTES
    ]


class TestRouteDispatch:
    def test_get_events(self):
        mock_fn = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        with patch.object(ah, "_ROUTES", _patched_routes("list_events", mock_fn)):
            result = ah.handler(_apigw("GET", "/v1/events"), None)
        assert result["statusCode"] == 200
        mock_fn.assert_called_once()

    def test_get_event_details(self):
        mock_fn = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        with patch.object(ah, "_ROUTES", _patched_routes("get_event_details", mock_fn)):
            result = ah.handler(_apigw("GET", "/v1/events/dGVzdA/details"), None)
        assert result["statusCode"] == 200
        mock_fn.assert_called_once()

    def test_get_summary(self):
        mock_fn = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        with patch.object(ah, "_ROUTES", _patched_routes("get_summary", mock_fn)):
            result = ah.handler(_apigw("GET", "/v1/summary"), None)
        assert result["statusCode"] == 200

    def test_get_orgs(self):
        mock_fn = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        with patch.object(ah, "_ROUTES", _patched_routes("list_orgs", mock_fn)):
            result = ah.handler(_apigw("GET", "/v1/orgs"), None)
        assert result["statusCode"] == 200

    def test_post_export(self):
        mock_fn = MagicMock(return_value={"statusCode": 202, "body": "{}"})
        with patch.object(ah, "_ROUTES", _patched_routes("trigger_export", mock_fn)):
            result = ah.handler(_apigw("POST", "/v1/export"), None)
        assert result["statusCode"] == 202

    def test_path_param_passed_for_event_details(self):
        captured = {}

        def _capture(query, multi, path_param):
            captured["path_param"] = path_param
            return {"statusCode": 200, "body": "{}"}

        with patch.object(ah, "_ROUTES", _patched_routes("get_event_details", _capture)):
            ah.handler(_apigw("GET", "/v1/events/abc123/details"), None)
        assert captured["path_param"] == "abc123"

    def test_query_params_forwarded(self):
        captured = {}

        def _capture(query, multi, path_param):
            captured["query"] = query
            return {"statusCode": 200, "body": "{}"}

        with patch.object(ah, "_ROUTES", _patched_routes("list_events", _capture)):
            ah.handler(_apigw("GET", "/v1/events", query={"category": "issue"}), None)
        assert captured["query"]["category"] == "issue"


# ── Error handling ────────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_unknown_path_returns_404(self):
        result = ah.handler(_apigw("GET", "/v1/unknown"), None)
        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"]["code"] == "NOT_FOUND"

    def test_wrong_method_returns_405(self):
        # /v1/events only accepts GET
        result = ah.handler(_apigw("POST", "/v1/events"), None)
        assert result["statusCode"] == 405
        body = json.loads(result["body"])
        assert body["error"]["code"] == "METHOD_NOT_ALLOWED"

    def test_wrong_method_for_summary(self):
        result = ah.handler(_apigw("DELETE", "/v1/summary"), None)
        assert result["statusCode"] == 405

    def test_value_error_returns_400(self):
        def _raise(query, multi, param):
            raise ValueError("bad param")

        with patch.object(ah, "_ROUTES", _patched_routes("list_events", _raise)):
            result = ah.handler(_apigw("GET", "/v1/events"), None)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"]["code"] == "INVALID_PARAMETER"
        assert "bad param" in body["error"]["message"]

    def test_unhandled_exception_returns_500(self):
        def _raise(query, multi, param):
            raise RuntimeError("boom")

        with patch.object(ah, "_ROUTES", _patched_routes("list_events", _raise)):
            result = ah.handler(_apigw("GET", "/v1/events"), None)
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"]["code"] == "INTERNAL_ERROR"

    def test_missing_path_defaults_to_slash(self):
        # handler should not crash if 'path' key missing
        event = {"httpMethod": "GET", "queryStringParameters": {}, "multiValueQueryStringParameters": {}}
        result = ah.handler(event, None)
        assert result["statusCode"] == 404
