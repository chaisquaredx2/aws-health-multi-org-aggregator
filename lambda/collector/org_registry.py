"""
org_registry.py — Load org configuration from SSM Parameter Store.
"""
import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

_SSM_PATH = os.environ.get("ORG_REGISTRY_PATH", "/health-aggregator/orgs")
_cache: list | None = None  # module-level cache (warm Lambda reuse)


def load_orgs(force_refresh: bool = False) -> list:
    """
    Return the list of enabled org configs from SSM.

    Caches the result for the lifetime of the Lambda execution environment.
    Pass force_refresh=True to bypass the in-memory cache.

    Schema of each item (see SPEC.md §4):
      org_id, org_name, delegated_admin_account_id,
      assume_role_arn, assume_role_external_id, enabled
    """
    global _cache
    if _cache is not None and not force_refresh:
        return _cache

    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=_SSM_PATH, WithDecryption=True)
    all_orgs: list = json.loads(resp["Parameter"]["Value"])
    _cache = [o for o in all_orgs if o.get("enabled", True)]
    logger.info("Loaded %d enabled org(s) from SSM %s", len(_cache), _SSM_PATH)
    return _cache
