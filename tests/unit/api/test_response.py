"""Unit tests for lambda/api/response.py"""
import json
from datetime import datetime

from response import response


class TestResponse:
    def test_status_code_200(self):
        assert response(200, {})["statusCode"] == 200

    def test_status_code_404(self):
        assert response(404, {})["statusCode"] == 404

    def test_status_code_500(self):
        assert response(500, {})["statusCode"] == 500

    def test_content_type_header(self):
        assert response(200, {})["headers"]["Content-Type"] == "application/json"

    def test_cors_header(self):
        assert response(200, {})["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_body_is_json_string(self):
        body = {"key": "value", "num": 42}
        parsed = json.loads(response(200, body)["body"])
        assert parsed == body

    def test_non_serializable_uses_default_str(self):
        dt = datetime(2026, 3, 15, 12, 0, 0)
        parsed = json.loads(response(200, {"ts": dt})["body"])
        assert "2026" in parsed["ts"]

    def test_nested_body(self):
        body = {"data": [{"id": 1}, {"id": 2}], "meta": {"total": 2}}
        parsed = json.loads(response(200, body)["body"])
        assert len(parsed["data"]) == 2
        assert parsed["meta"]["total"] == 2

    def test_empty_body(self):
        parsed = json.loads(response(204, {})["body"])
        assert parsed == {}
