"""
handler.py — API Lambda entry point.

Routes API Gateway proxy events to the correct handler function based on
the request path and HTTP method. All routes return the standard response envelope.

Routes:
  GET  /v1/events                          -> routes.events.list_events
  GET  /v1/events/{event_arn_b64}/details  -> routes.events.get_event_details
  GET  /v1/summary                         -> routes.summary.get_summary
  GET  /v1/orgs                            -> routes.orgs.list_orgs
  POST /v1/export                          -> routes.export.trigger_export
"""

import json
import logging
import os
import re

from routes.events import list_events, get_event_details
from routes.export import trigger_export
from routes.summary import get_summary
from routes.orgs import list_orgs

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# (method, path_pattern, handler_fn, has_path_param)
_ROUTES = [
    ("GET",  re.compile(r"^/v1/events/([^/]+)/details$"), get_event_details, True),
    ("GET",  re.compile(r"^/v1/events$"),                  list_events,       False),
    ("GET",  re.compile(r"^/v1/summary$"),                 get_summary,       False),
    ("GET",  re.compile(r"^/v1/orgs$"),                    list_orgs,         False),
    ("POST", re.compile(r"^/v1/export$"),                  trigger_export,    False),
]


def handler(event: dict, context) -> dict:
    path = event.get("path", "/")
    method = event.get("httpMethod", "GET")
    query = event.get("queryStringParameters") or {}
    multi_query = event.get("multiValueQueryStringParameters") or {}

    if method == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": {
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type,X-Api-Key,Authorization",
            },
            "body": "",
        }

    for route_method, pattern, fn, has_param in _ROUTES:
        m = pattern.match(path)
        if m:
            if method != route_method:
                return _response(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": f"{path} only accepts {route_method}"}})
            try:
                path_param = m.group(1) if has_param else None
                return fn(query, multi_query, path_param)
            except ValueError as exc:
                return _response(400, {"error": {"code": "INVALID_PARAMETER", "message": str(exc)}})
            except Exception as exc:
                logger.exception("Unhandled error in %s", fn.__name__)
                return _response(500, {"error": {"code": "INTERNAL_ERROR", "message": str(exc)}})

    return _response(404, {"error": {"code": "NOT_FOUND", "message": f"No route for {method} {path}"}})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }
