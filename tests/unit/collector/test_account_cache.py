"""Unit tests for lambda/collector/account_cache.py"""
import time
import pytest
from unittest.mock import patch, MagicMock, call

import account_cache


_CREDS = {"AccessKeyId": "AKID", "SecretAccessKey": "SECRET", "SessionToken": "TOKEN"}


class TestFetchAccountTags:
    def test_returns_tags_as_dict(self):
        mock_client = MagicMock()
        mock_client.list_tags_for_resource.return_value = {
            "Tags": [
                {"Key": "BusinessUnit", "Value": "Engineering"},
                {"Key": "Environment", "Value": "production"},
            ]
        }
        result = account_cache._fetch_account_tags("123456789012", mock_client)
        assert result == {"BusinessUnit": "Engineering", "Environment": "production"}

    def test_returns_empty_dict_on_exception(self):
        mock_client = MagicMock()
        mock_client.list_tags_for_resource.side_effect = Exception("AccessDenied")
        result = account_cache._fetch_account_tags("123456789012", mock_client)
        assert result == {}

    def test_returns_empty_dict_when_no_tags(self):
        mock_client = MagicMock()
        mock_client.list_tags_for_resource.return_value = {"Tags": []}
        assert account_cache._fetch_account_tags("123456789012", mock_client) == {}


class TestListAccounts:
    def test_filters_to_active_accounts(self):
        with patch("account_cache.boto3.client") as mock_boto:
            mock_orgs = MagicMock()
            mock_boto.return_value = mock_orgs
            mock_paginator = MagicMock()
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_paginator.paginate.return_value = [{
                "Accounts": [
                    {"Id": "111", "Name": "Active", "Status": "ACTIVE"},
                    {"Id": "222", "Name": "Suspended", "Status": "SUSPENDED"},
                    {"Id": "333", "Name": "Closed", "Status": "CLOSED"},
                ]
            }]
            result = account_cache._list_accounts(_CREDS)
        assert len(result) == 1
        assert result[0]["Id"] == "111"

    def test_uses_assumed_credentials(self):
        with patch("account_cache.boto3.client") as mock_boto:
            mock_orgs = MagicMock()
            mock_boto.return_value = mock_orgs
            mock_paginator = MagicMock()
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_paginator.paginate.return_value = [{"Accounts": []}]
            account_cache._list_accounts(_CREDS)
        mock_boto.assert_called_once_with(
            "organizations",
            aws_access_key_id="AKID",
            aws_secret_access_key="SECRET",
            aws_session_token="TOKEN",
        )

    def test_aggregates_multiple_pages(self):
        with patch("account_cache.boto3.client") as mock_boto:
            mock_orgs = MagicMock()
            mock_boto.return_value = mock_orgs
            mock_paginator = MagicMock()
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_paginator.paginate.return_value = [
                {"Accounts": [{"Id": "111", "Name": "A", "Status": "ACTIVE"}]},
                {"Accounts": [{"Id": "222", "Name": "B", "Status": "ACTIVE"}]},
            ]
            result = account_cache._list_accounts(_CREDS)
        assert len(result) == 2


class TestEnrichAndCache:
    def test_fetches_tags_and_writes_to_cache(self):
        accounts = [
            {"Id": "111111111111", "Name": "MyAccount", "Status": "ACTIVE"},
        ]
        with patch("account_cache.boto3.client") as mock_boto, \
             patch("account_cache._table") as mock_table:
            mock_orgs = MagicMock()
            mock_boto.return_value = mock_orgs
            mock_orgs.list_tags_for_resource.return_value = {
                "Tags": [
                    {"Key": "BusinessUnit", "Value": "Platform"},
                    {"Key": "Environment", "Value": "production"},
                ]
            }
            mock_batch = MagicMock()
            mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch
            mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

            result = account_cache._enrich_and_cache("o-xxx", accounts, _CREDS)

        assert "111111111111" in result
        assert result["111111111111"]["account_name"] == "MyAccount"
        assert result["111111111111"]["business_unit"] == "Platform"
        assert result["111111111111"]["environment"] == "production"

    def test_uses_defaults_when_tags_missing(self):
        accounts = [{"Id": "999888777666", "Name": "NoTags"}]
        with patch("account_cache.boto3.client") as mock_boto, \
             patch("account_cache._table") as mock_table:
            mock_orgs = MagicMock()
            mock_boto.return_value = mock_orgs
            mock_orgs.list_tags_for_resource.return_value = {"Tags": []}
            mock_batch = MagicMock()
            mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch
            mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

            result = account_cache._enrich_and_cache("o-xxx", accounts, _CREDS)

        assert result["999888777666"]["business_unit"] == "Unknown"
        assert result["999888777666"]["environment"] == "non-production"


class TestScanOrgCache:
    def test_returns_items_for_org(self):
        with patch("account_cache._table") as mock_table:
            mock_table.scan.return_value = {
                "Items": [
                    {"pk": "o-xxx#111", "account_id": "111", "account_name": "A",
                     "business_unit": "Eng", "environment": "prod", "ttl": int(time.time()) + 3600}
                ]
            }
            items = account_cache._scan_org_cache("o-xxx")
        assert len(items) == 1

    def test_handles_pagination(self):
        with patch("account_cache._table") as mock_table:
            mock_table.scan.side_effect = [
                {"Items": [{"pk": "o-xxx#111", "account_id": "111", "account_name": "A",
                            "business_unit": "Eng", "environment": "prod", "ttl": 9999999999}],
                 "LastEvaluatedKey": {"pk": "o-xxx#111"}},
                {"Items": [{"pk": "o-xxx#222", "account_id": "222", "account_name": "B",
                            "business_unit": "Eng", "environment": "prod", "ttl": 9999999999}]},
            ]
            items = account_cache._scan_org_cache("o-xxx")
        assert len(items) == 2


class TestLoadAccountMap:
    @patch("account_cache._scan_org_cache")
    @patch("account_cache._list_accounts")
    def test_returns_cached_items_when_warm(self, mock_list, mock_scan):
        now = int(time.time())
        mock_scan.return_value = [{
            "account_id": "111", "account_name": "A",
            "business_unit": "Eng", "environment": "prod",
            "ttl": now + 3600,
        }]
        mock_list.return_value = [{"Id": "111", "Name": "A", "Status": "ACTIVE"}]

        result = account_cache.load_account_map("o-xxx", _CREDS)
        assert "111" in result
        assert result["111"]["account_name"] == "A"

    @patch("account_cache._scan_org_cache")
    @patch("account_cache._list_accounts")
    @patch("account_cache._enrich_and_cache")
    def test_calls_enrich_on_cache_miss(self, mock_enrich, mock_list, mock_scan):
        mock_scan.return_value = []
        mock_list.return_value = [{"Id": "111", "Name": "A", "Status": "ACTIVE"}]
        mock_enrich.return_value = {"111": {"account_name": "A", "business_unit": "Eng", "environment": "prod"}}

        result = account_cache.load_account_map("o-xxx", _CREDS)
        mock_enrich.assert_called_once()
        assert "111" in result

    @patch("account_cache._scan_org_cache")
    @patch("account_cache._list_accounts")
    @patch("account_cache._enrich_and_cache")
    def test_calls_enrich_on_expired_cache(self, mock_enrich, mock_list, mock_scan):
        now = int(time.time())
        mock_scan.return_value = [{
            "account_id": "111", "account_name": "Old",
            "business_unit": "X", "environment": "prod",
            "ttl": now - 1,  # expired
        }]
        mock_list.return_value = [{"Id": "111", "Name": "New", "Status": "ACTIVE"}]
        mock_enrich.return_value = {"111": {"account_name": "New", "business_unit": "X", "environment": "prod"}}

        account_cache.load_account_map("o-xxx", _CREDS)
        mock_enrich.assert_called_once()
