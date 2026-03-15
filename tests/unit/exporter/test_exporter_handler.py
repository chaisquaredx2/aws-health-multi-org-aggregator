"""Unit tests for lambda/exporter/handler.py"""
import json
import sys
import pytest
from unittest.mock import patch, MagicMock

import exporter_handler as eh


def _event_item(arn="arn::1", status="open"):
    return {
        "event_arn": arn,
        "org_id": "o-aaa",
        "org_name": "Org A",
        "category": "issue",
        "service": "EC2",
        "event_type_code": "AWS_EC2_ISSUE",
        "region": "us-east-1",
        "status": status,
        "severity": "standard",
        "is_operational": True,
        "start_time": "2026-01-01T12:00:00+00:00",
        "last_updated_time": "2026-01-01T13:00:00+00:00",
        "affected_account_count": 1,
        "affected_accounts": [{"account_id": "111"}],
    }


# ── _scan_events ──────────────────────────────────────────────────────────────

class TestScanEvents:
    def test_returns_items(self):
        with patch.object(eh, "_table") as mock_table:
            mock_table.scan.return_value = {"Items": [_event_item()]}
            result = eh._scan_events("2026-01-01T00:00:00Z")
        assert len(result) == 1

    def test_follows_pagination(self):
        item1 = _event_item("arn::1")
        item2 = _event_item("arn::2")
        with patch.object(eh, "_table") as mock_table:
            mock_table.scan.side_effect = [
                {"Items": [item1], "LastEvaluatedKey": {"pk": "x"}},
                {"Items": [item2]},
            ]
            result = eh._scan_events("2026-01-01T00:00:00Z")
        assert len(result) == 2

    def test_empty_table(self):
        with patch.object(eh, "_table") as mock_table:
            mock_table.scan.return_value = {"Items": []}
            result = eh._scan_events("2026-01-01T00:00:00Z")
        assert result == []


# ── _load_json_from_s3 ────────────────────────────────────────────────────────

class TestLoadJsonFromS3:
    def test_returns_parsed_json(self):
        payload = [{"arn": "arn::1"}]
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(payload).encode())
        }
        with patch.object(eh, "_s3", mock_s3):
            result = eh._load_json_from_s3("some/key.json", default=[])
        assert result == payload

    def test_returns_default_on_no_such_key(self):
        mock_s3 = MagicMock()
        # Simulate S3 NoSuchKey by raising the correct exception type
        no_such_key_exc = mock_s3.exceptions.NoSuchKey = type(
            "NoSuchKey", (Exception,), {}
        )
        mock_s3.get_object.side_effect = no_such_key_exc("not found")
        with patch.object(eh, "_s3", mock_s3):
            result = eh._load_json_from_s3("missing.json", default={"x": 1})
        assert result == {"x": 1}

    def test_returns_default_on_generic_error(self):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("connection error")
        # Ensure exception.NoSuchKey is a different type
        mock_s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        with patch.object(eh, "_s3", mock_s3):
            result = eh._load_json_from_s3("key.json", default=[])
        assert result == []


# ── _save_json_to_s3 ──────────────────────────────────────────────────────────

class TestSaveJsonToS3:
    def test_calls_put_object(self):
        with patch.object(eh, "_s3") as mock_s3:
            eh._save_json_to_s3("key.json", {"data": 1})
        mock_s3.put_object.assert_called_once()

    def test_swallows_exception(self):
        with patch.object(eh, "_s3") as mock_s3:
            mock_s3.put_object.side_effect = Exception("S3 error")
            eh._save_json_to_s3("key.json", [])  # must not raise

    def test_content_type_is_json(self):
        with patch.object(eh, "_s3") as mock_s3:
            eh._save_json_to_s3("key.json", {"x": 1})
        kwargs = mock_s3.put_object.call_args.kwargs
        assert kwargs["ContentType"] == "application/json"

    def test_key_passed_to_put_object(self):
        with patch.object(eh, "_s3") as mock_s3:
            eh._save_json_to_s3("my/path/state.json", [])
        kwargs = mock_s3.put_object.call_args.kwargs
        assert kwargs["Key"] == "my/path/state.json"


# ── handler ───────────────────────────────────────────────────────────────────

class TestHandler:
    def _make_mocks(self, items=None):
        items = items or [_event_item()]
        return items

    # Handler tests let the real excel_writer functions run; only AWS calls are mocked.
    # _build_dataframes/_compute_delta/_build_delta_log are deferred imports inside
    # handler() so we let them run against real pandas rather than trying to patch them.

    def _handler_patches(self, items, xlsx_bytes=b"PK\x03\x04fake"):
        return (
            patch("exporter_handler._scan_events", return_value=items),
            patch("exporter_handler._load_json_from_s3", return_value=[]),
            patch("exporter_handler.write_excel", return_value=xlsx_bytes),
            patch("exporter_handler.current_open_arns", return_value=[]),
            patch("exporter_handler._save_json_to_s3"),
            patch.object(eh, "_s3"),
        )

    def test_returns_200_on_success(self):
        items = [_event_item()]
        with patch("exporter_handler._scan_events", return_value=items), \
             patch("exporter_handler._load_json_from_s3", return_value=[]), \
             patch("exporter_handler.write_excel", return_value=b"PK\x03\x04"), \
             patch("exporter_handler.current_open_arns", return_value=[]), \
             patch("exporter_handler._save_json_to_s3"), \
             patch.object(eh, "_s3"):
            result = eh.handler({}, None)
        assert result["statusCode"] == 200

    def test_result_contains_events_exported(self):
        items = [_event_item()]
        with patch("exporter_handler._scan_events", return_value=items), \
             patch("exporter_handler._load_json_from_s3", return_value=[]), \
             patch("exporter_handler.write_excel", return_value=b"PK\x03\x04"), \
             patch("exporter_handler.current_open_arns", return_value=[]), \
             patch("exporter_handler._save_json_to_s3"), \
             patch.object(eh, "_s3"):
            result = eh.handler({}, None)
        assert result["events_exported"] == 1

    def test_uploads_excel_to_s3(self):
        items = [_event_item()]
        xlsx_bytes = b"PK\x03\x04fake"
        with patch("exporter_handler._scan_events", return_value=items), \
             patch("exporter_handler._load_json_from_s3", return_value=[]), \
             patch("exporter_handler.write_excel", return_value=xlsx_bytes), \
             patch("exporter_handler.current_open_arns", return_value=[]), \
             patch("exporter_handler._save_json_to_s3"), \
             patch.object(eh, "_s3") as mock_s3:
            eh.handler({}, None)
        mock_s3.put_object.assert_called_once()
        kwargs = mock_s3.put_object.call_args.kwargs
        assert kwargs["Body"] == xlsx_bytes

    def test_s3_key_includes_date(self):
        from datetime import datetime, timezone
        items = [_event_item()]
        with patch("exporter_handler._scan_events", return_value=items), \
             patch("exporter_handler._load_json_from_s3", return_value=[]), \
             patch("exporter_handler.write_excel", return_value=b"PK\x03\x04"), \
             patch("exporter_handler.current_open_arns", return_value=[]), \
             patch("exporter_handler._save_json_to_s3"), \
             patch.object(eh, "_s3"):
            result = eh.handler({}, None)
        today = datetime.now(timezone.utc).strftime("%Y")
        assert today in result["report_s3_key"]
