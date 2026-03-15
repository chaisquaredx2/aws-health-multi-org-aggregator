"""
account_cache.py — Per-org account metadata cache backed by DynamoDB.

Reduces Organizations API calls from O(accounts × runs) to O(accounts/24h).

Cache table: health-aggregator-account-metadata
  PK: "{org_id}#{account_id}"   (no sort key)
  TTL: 24 hours from cached_at

On a cold cache run, all accounts are fetched from Organizations and written.
On warm runs, only newly created accounts (cache miss) trigger Org API calls.
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

_TABLE_NAME = os.environ["ACCOUNT_METADATA_TABLE_NAME"]
_CACHE_TTL_HOURS = int(os.environ.get("ACCOUNT_CACHE_TTL_HOURS", "24"))

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME)


def _pk(org_id: str, account_id: str) -> str:
    return f"{org_id}#{account_id}"


def load_account_map(
    org_id: str,
    assumed_credentials: dict,
) -> dict:
    """
    Return {account_id: {account_name, business_unit, environment}} for org_id.

    Strategy:
      1. Scan cache table for all items where pk begins_with "{org_id}#".
      2. Split into cache-hits (ttl > now) and cache-misses.
      3. For cache-misses: call Organizations API with assumed_credentials,
         fetch account name + tags, write entries with fresh TTL.
      4. Return the merged account_map.

    Args:
        org_id:              AWS Organization ID (o-xxxx).
        assumed_credentials: Dict with AccessKeyId, SecretAccessKey,
                             SessionToken — from sts.assume_role().
    """
    now_epoch = int(time.time())

    # Step 1: read what we have cached for this org
    cached_items = _scan_org_cache(org_id)
    hit_map: dict = {}
    miss_account_ids: set = set()

    for item in cached_items:
        account_id = item["account_id"]
        ttl = int(item.get("ttl", 0))
        if ttl > now_epoch:
            hit_map[account_id] = {
                "account_name": item["account_name"],
                "business_unit": item.get("business_unit", "Unknown"),
                "environment": item.get("environment", "non-production"),
            }
        else:
            miss_account_ids.add(account_id)

    # Step 2: fetch fresh data for misses + any accounts not yet in cache
    all_org_accounts = _list_accounts(assumed_credentials)

    # Accounts not in cache at all are also misses
    cached_ids = {i["account_id"] for i in cached_items}
    for acct in all_org_accounts:
        if acct["Id"] not in cached_ids:
            miss_account_ids.add(acct["Id"])

    # Step 3: enrich misses and write back to cache
    if miss_account_ids:
        miss_accounts = [a for a in all_org_accounts if a["Id"] in miss_account_ids]
        fresh_entries = _enrich_and_cache(org_id, miss_accounts, assumed_credentials)
        hit_map.update(fresh_entries)

    logger.info(
        "account_map for %s: %d from cache, %d freshly fetched",
        org_id, len(cached_items) - len(miss_account_ids), len(miss_account_ids),
    )
    return hit_map


def _scan_org_cache(org_id: str) -> list:
    """Scan DynamoDB for all cached accounts belonging to org_id."""
    prefix = f"{org_id}#"
    items: list = []
    kwargs: dict = {
        "FilterExpression": "begins_with(pk, :pfx)",
        "ExpressionAttributeValues": {":pfx": prefix},
    }
    while True:
        resp = _table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _list_accounts(assumed_credentials: dict) -> list:
    """List all ACTIVE accounts in the org using assumed role credentials."""
    orgs = boto3.client(
        "organizations",
        aws_access_key_id=assumed_credentials["AccessKeyId"],
        aws_secret_access_key=assumed_credentials["SecretAccessKey"],
        aws_session_token=assumed_credentials["SessionToken"],
    )
    accounts: list = []
    paginator = orgs.get_paginator("list_accounts")
    for page in paginator.paginate():
        accounts.extend(
            [a for a in page["Accounts"] if a["Status"] == "ACTIVE"]
        )
    return accounts


def _fetch_account_tags(account_id: str, orgs_client) -> dict:
    """Fetch BusinessUnit and Environment tags for one account. Returns {} on error."""
    try:
        tag_resp = orgs_client.list_tags_for_resource(ResourceId=account_id)
        return {t["Key"]: t["Value"] for t in tag_resp.get("Tags", [])}
    except Exception as exc:
        logger.warning("list_tags_for_resource(%s): %s", account_id, exc)
        return {}


def _enrich_and_cache(
    org_id: str,
    accounts: list,
    assumed_credentials: dict,
) -> dict:
    """
    Fetch BusinessUnit + Environment tags for each account, write to cache.
    Returns {account_id: metadata} for the freshly fetched accounts.
    """
    orgs = boto3.client(
        "organizations",
        aws_access_key_id=assumed_credentials["AccessKeyId"],
        aws_secret_access_key=assumed_credentials["SecretAccessKey"],
        aws_session_token=assumed_credentials["SessionToken"],
    )

    result: dict = {}
    ttl = int(time.time()) + (_CACHE_TTL_HOURS * 3600)
    now_iso = datetime.now(timezone.utc).isoformat()

    with _table.batch_writer() as batch:
        for account in accounts:
            account_id = account["Id"]
            account_name = account["Name"]

            tags = _fetch_account_tags(account_id, orgs)
            business_unit = tags.get("BusinessUnit", "Unknown")
            environment = tags.get("Environment", "non-production")

            result[account_id] = {
                "account_name": account_name,
                "business_unit": business_unit,
                "environment": environment,
            }

            batch.put_item(Item={
                "pk": _pk(org_id, account_id),
                "org_id": org_id,
                "account_id": account_id,
                "account_name": account_name,
                "business_unit": business_unit,
                "environment": environment,
                "cached_at": now_iso,
                "ttl": ttl,
            })

    logger.info("Cached %d account entries for org %s", len(result), org_id)
    return result
