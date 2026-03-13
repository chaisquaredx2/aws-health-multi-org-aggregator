"""
routes/orgs.py — GET /v1/orgs
"""

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

_STATE_TABLE_NAME = os.environ["STATE_TABLE_NAME"]
_ORG_REGISTRY_PATH = os.environ.get("ORG_REGISTRY_PATH", "/health-aggregator/orgs")

_dynamodb = boto3.resource("dynamodb")
_state_table = _dynamodb.Table(_STATE_TABLE_NAME)


def list_orgs(query: dict, _multi: dict, _param=None) -> dict:
    # Load org registry from SSM for names + config
    import json as _json
    ssm = boto3.client("ssm")
    param = ssm.get_parameter(Name=_ORG_REGISTRY_PATH, WithDecryption=True)
    all_orgs: list = _json.loads(param["Parameter"]["Value"])

    # Load collection state for each org from DynamoDB
    org_ids = [o["org_id"] for o in all_orgs]
    state_map: dict = {}
    if org_ids:
        keys = [{"pk": oid} for oid in org_ids]
        resp = _dynamodb.batch_get_item(
            RequestItems={_STATE_TABLE_NAME: {"Keys": keys}}
        )
        for item in resp.get("Responses", {}).get(_STATE_TABLE_NAME, []):
            state_map[item["pk"]] = item

    data = []
    for org in all_orgs:
        oid = org["org_id"]
        state = state_map.get(oid, {})
        data.append({
            "org_id": oid,
            "org_name": org.get("org_name", ""),
            "delegated_admin_account_id": org.get("delegated_admin_account_id", ""),
            "enabled": org.get("enabled", True),
            "collection": {
                "last_successful_at": state.get("last_successful_at"),
                "last_attempted_at": state.get("last_attempted_at"),
                "last_error": state.get("last_error"),
                "events_in_window": int(state.get("events_in_window", 0)),
            },
        })

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"data": data}, default=str),
    }
