"""Unit tests for lambda/api/routes/orgs.py"""
import json
import pytest
from unittest.mock import patch, MagicMock

from orgs import list_orgs


def _ssm_param(orgs_list):
    return {"Parameter": {"Value": json.dumps(orgs_list)}}


def _state_item(org_id, last_successful_at="2026-01-01T12:00:00Z", events=5):
    return {
        "pk": org_id,
        "org_id": org_id,
        "last_successful_at": last_successful_at,
        "last_attempted_at": last_successful_at,
        "events_in_window": events,
    }


class TestListOrgs:
    def _mock_aws(self, orgs_list, state_items=None):
        """Return a context-manager-compatible pair of SSM + DynamoDB mocks."""
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = _ssm_param(orgs_list)

        mock_ddb = MagicMock()
        table_name = "test-state"
        mock_ddb.batch_get_item.return_value = {
            "Responses": {table_name: state_items or []}
        }
        return mock_ssm, mock_ddb

    def test_returns_200(self):
        ssm, ddb = self._mock_aws([{"org_id": "o-aaa", "org_name": "Org A"}])
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            result = list_orgs({}, {})
        assert result["statusCode"] == 200

    def test_returns_all_orgs(self):
        orgs = [
            {"org_id": "o-aaa", "org_name": "Org A"},
            {"org_id": "o-bbb", "org_name": "Org B"},
        ]
        ssm, ddb = self._mock_aws(orgs)
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            result = list_orgs({}, {})
        body = json.loads(result["body"])
        assert len(body["data"]) == 2

    def test_merges_collection_state(self):
        org = {"org_id": "o-aaa", "org_name": "Org A"}
        state = _state_item("o-aaa", events=42)
        ssm, ddb = self._mock_aws([org], [state])
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            result = list_orgs({}, {})
        body = json.loads(result["body"])
        assert body["data"][0]["collection"]["events_in_window"] == 42
        assert body["data"][0]["collection"]["last_successful_at"] == "2026-01-01T12:00:00Z"

    def test_defaults_when_no_state(self):
        org = {"org_id": "o-aaa", "org_name": "Org A"}
        ssm, ddb = self._mock_aws([org], [])  # no state in DDB
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            result = list_orgs({}, {})
        body = json.loads(result["body"])
        coll = body["data"][0]["collection"]
        assert coll["events_in_window"] == 0
        assert coll["last_successful_at"] is None

    def test_includes_enabled_field(self):
        org = {"org_id": "o-aaa", "org_name": "Org A", "enabled": False}
        ssm, ddb = self._mock_aws([org])
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            result = list_orgs({}, {})
        body = json.loads(result["body"])
        assert body["data"][0]["enabled"] is False

    def test_enabled_defaults_to_true(self):
        org = {"org_id": "o-aaa", "org_name": "Org A"}  # no enabled field
        ssm, ddb = self._mock_aws([org])
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            result = list_orgs({}, {})
        body = json.loads(result["body"])
        assert body["data"][0]["enabled"] is True

    def test_empty_org_list(self):
        ssm, ddb = self._mock_aws([])
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            result = list_orgs({}, {})
        body = json.loads(result["body"])
        assert body["data"] == []

    def test_calls_ssm_with_correct_path(self):
        ssm, ddb = self._mock_aws([])
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            list_orgs({}, {})
        ssm.get_parameter.assert_called_once_with(
            Name="/health-aggregator/orgs", WithDecryption=True
        )

    def test_last_error_included_in_collection(self):
        org = {"org_id": "o-aaa", "org_name": "Org A"}
        state = {**_state_item("o-aaa"), "last_error": "AccessDenied"}
        ssm, ddb = self._mock_aws([org], [state])
        with patch("orgs.boto3.client", return_value=ssm), \
             patch("orgs._dynamodb", ddb):
            result = list_orgs({}, {})
        body = json.loads(result["body"])
        assert body["data"][0]["collection"]["last_error"] == "AccessDenied"
