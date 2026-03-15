"""Unit tests for lambda/collector/org_registry.py"""
import json
import pytest
from unittest.mock import patch, MagicMock

import org_registry


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset module-level cache before each test."""
    org_registry._cache = None
    yield
    org_registry._cache = None


def _ssm_resp(orgs):
    return {"Parameter": {"Value": json.dumps(orgs)}}


class TestLoadOrgs:
    @patch("org_registry.boto3.client")
    def test_returns_enabled_orgs_only(self, mock_boto):
        orgs = [
            {"org_id": "o-aaa", "org_name": "Org A", "enabled": True},
            {"org_id": "o-bbb", "org_name": "Org B", "enabled": False},
        ]
        mock_boto.return_value.get_parameter.return_value = _ssm_resp(orgs)
        result = org_registry.load_orgs()
        assert len(result) == 1
        assert result[0]["org_id"] == "o-aaa"

    @patch("org_registry.boto3.client")
    def test_missing_enabled_field_defaults_to_true(self, mock_boto):
        orgs = [{"org_id": "o-ccc", "org_name": "Org C"}]
        mock_boto.return_value.get_parameter.return_value = _ssm_resp(orgs)
        result = org_registry.load_orgs()
        assert len(result) == 1

    @patch("org_registry.boto3.client")
    def test_caches_result_on_second_call(self, mock_boto):
        orgs = [{"org_id": "o-aaa", "org_name": "Org A"}]
        mock_boto.return_value.get_parameter.return_value = _ssm_resp(orgs)
        org_registry.load_orgs()
        org_registry.load_orgs()
        assert mock_boto.return_value.get_parameter.call_count == 1

    @patch("org_registry.boto3.client")
    def test_force_refresh_bypasses_cache(self, mock_boto):
        orgs = [{"org_id": "o-aaa", "org_name": "Org A"}]
        mock_boto.return_value.get_parameter.return_value = _ssm_resp(orgs)
        org_registry.load_orgs()
        org_registry.load_orgs(force_refresh=True)
        assert mock_boto.return_value.get_parameter.call_count == 2

    @patch("org_registry.boto3.client")
    def test_all_disabled_returns_empty_list(self, mock_boto):
        orgs = [{"org_id": "o-aaa", "enabled": False}]
        mock_boto.return_value.get_parameter.return_value = _ssm_resp(orgs)
        assert org_registry.load_orgs() == []

    @patch("org_registry.boto3.client")
    def test_empty_org_list(self, mock_boto):
        mock_boto.return_value.get_parameter.return_value = _ssm_resp([])
        assert org_registry.load_orgs() == []

    @patch("org_registry.boto3.client")
    def test_calls_ssm_with_correct_path(self, mock_boto):
        mock_boto.return_value.get_parameter.return_value = _ssm_resp([])
        org_registry.load_orgs()
        mock_boto.return_value.get_parameter.assert_called_once_with(
            Name="/health-aggregator/orgs", WithDecryption=True
        )

    @patch("org_registry.boto3.client")
    def test_multiple_enabled_orgs(self, mock_boto):
        orgs = [
            {"org_id": "o-1", "org_name": "Org 1"},
            {"org_id": "o-2", "org_name": "Org 2"},
            {"org_id": "o-3", "org_name": "Org 3", "enabled": False},
        ]
        mock_boto.return_value.get_parameter.return_value = _ssm_resp(orgs)
        result = org_registry.load_orgs()
        assert len(result) == 2
        assert {o["org_id"] for o in result} == {"o-1", "o-2"}
