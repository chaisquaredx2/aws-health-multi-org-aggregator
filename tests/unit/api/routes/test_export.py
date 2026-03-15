"""Unit tests for lambda/api/routes/export.py"""
import json
import pytest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

from export import trigger_export


# ── trigger_export ────────────────────────────────────────────────────────────

class TestTriggerExport:
    def test_returns_501_when_not_configured(self):
        with patch("export._EXPORTER_FUNCTION_NAME", ""):
            result = trigger_export({}, {})
        assert result["statusCode"] == 501
        body = json.loads(result["body"])
        assert body["error"]["code"] == "NOT_CONFIGURED"

    def test_returns_202_on_success(self):
        with patch("export._EXPORTER_FUNCTION_NAME", "my-exporter-fn"), \
             patch("export._lambda_client") as mock_lambda:
            mock_lambda.invoke.return_value = {"StatusCode": 202}
            result = trigger_export({}, {})
        assert result["statusCode"] == 202

    def test_response_body_contains_message(self):
        with patch("export._EXPORTER_FUNCTION_NAME", "my-exporter-fn"), \
             patch("export._lambda_client") as mock_lambda:
            mock_lambda.invoke.return_value = {"StatusCode": 202}
            result = trigger_export({}, {})
        body = json.loads(result["body"])
        assert "message" in body
        assert "estimated_s3_key" in body

    def test_s3_uri_included_when_bucket_set(self):
        with patch("export._EXPORTER_FUNCTION_NAME", "my-exporter-fn"), \
             patch("export._EXPORT_BUCKET", "my-bucket"), \
             patch("export._lambda_client") as mock_lambda:
            mock_lambda.invoke.return_value = {"StatusCode": 202}
            result = trigger_export({}, {})
        body = json.loads(result["body"])
        assert "s3_uri" in body
        assert body["s3_uri"].startswith("s3://my-bucket/")

    def test_s3_uri_absent_when_bucket_empty(self):
        with patch("export._EXPORTER_FUNCTION_NAME", "my-exporter-fn"), \
             patch("export._EXPORT_BUCKET", ""), \
             patch("export._lambda_client") as mock_lambda:
            mock_lambda.invoke.return_value = {"StatusCode": 202}
            result = trigger_export({}, {})
        body = json.loads(result["body"])
        assert "s3_uri" not in body

    def test_invokes_lambda_async(self):
        with patch("export._EXPORTER_FUNCTION_NAME", "my-exporter-fn"), \
             patch("export._lambda_client") as mock_lambda:
            mock_lambda.invoke.return_value = {"StatusCode": 202}
            trigger_export({}, {})
        kwargs = mock_lambda.invoke.call_args.kwargs
        assert kwargs["InvocationType"] == "Event"
        assert kwargs["FunctionName"] == "my-exporter-fn"

    def test_returns_500_on_client_error(self):
        with patch("export._EXPORTER_FUNCTION_NAME", "my-exporter-fn"), \
             patch("export._lambda_client") as mock_lambda:
            mock_lambda.invoke.side_effect = ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "fn not found"}},
                "Invoke",
            )
            result = trigger_export({}, {})
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"]["code"] == "INTERNAL_ERROR"

    def test_s3_key_contains_today_date(self):
        from datetime import datetime, timezone
        with patch("export._EXPORTER_FUNCTION_NAME", "my-fn"), \
             patch("export._lambda_client") as mock_lambda:
            mock_lambda.invoke.return_value = {"StatusCode": 202}
            result = trigger_export({}, {})
        body = json.loads(result["body"])
        today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        assert today in body["estimated_s3_key"]
