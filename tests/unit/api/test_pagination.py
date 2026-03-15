"""Unit tests for lambda/api/pagination.py"""
import base64
import json

import pytest

from pagination import decode_token, encode_token


class TestEncodeToken:
    def test_returns_string(self):
        assert isinstance(encode_token({"pk": "abc"}), str)

    def test_is_decodable_base64(self):
        token = encode_token({"pk": "abc"})
        base64.b64decode(token)  # must not raise

    def test_encodes_single_key(self):
        key = {"pk": "some-pk"}
        token = encode_token(key)
        assert json.loads(base64.b64decode(token)) == key

    def test_encodes_composite_key(self):
        key = {"pk": "arn::aws::123", "sk": "issue#2026-01-01T00:00:00"}
        token = encode_token(key)
        assert json.loads(base64.b64decode(token)) == key

    def test_encode_decode_roundtrip(self):
        key = {"pk": "arn::aws::456", "category-starttime-index": "val"}
        assert decode_token(encode_token(key)) == key


class TestDecodeToken:
    def test_decodes_known_token(self):
        key = {"pk": "test-pk", "sk": "test-sk"}
        token = base64.b64encode(json.dumps(key).encode()).decode()
        assert decode_token(token) == key

    def test_decodes_unicode_values(self):
        key = {"pk": "arn:aws:health:us-east-1::event/EC2/AWS_EC2_OPERATIONAL_ISSUE/abc123"}
        assert decode_token(encode_token(key)) == key

    def test_raises_on_invalid_base64(self):
        with pytest.raises(Exception):
            decode_token("not-valid-base64!!!")

    def test_raises_on_invalid_json(self):
        bad_token = base64.b64encode(b"not-json").decode()
        with pytest.raises(Exception):
            decode_token(bad_token)
