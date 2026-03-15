"""
Global test configuration.

Sets required environment variables and sys.path entries before any Lambda
module is imported. The boto3 module-level client/resource instantiations in
each handler.py are safe (they do not make network calls), but they require the
env vars to exist. Moto mocks intercept at the HTTP layer, so they work
correctly even when the client is instantiated outside the mock context.
"""

import importlib.util
import os
import sys

# ── Required env vars (module-level reads in Lambda handlers) ─────────────────
os.environ.setdefault("TABLE_NAME", "test-events")
os.environ.setdefault("STATE_TABLE_NAME", "test-state")
os.environ.setdefault("ACCOUNT_METADATA_TABLE_NAME", "test-account-metadata")
os.environ.setdefault("HEALTH_PROXY_API_URL", "https://test.execute-api.us-east-1.amazonaws.com/prod")
os.environ.setdefault("EXPORT_BUCKET", "test-export-bucket")
os.environ.setdefault("ORG_REGISTRY_PATH", "/health-aggregator/orgs")
os.environ.setdefault("HEALTH_ALERT_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:test-alerts")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

# ── sys.path entries per Lambda package ───────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(__file__))
_LAMBDA = os.path.join(_ROOT, "lambda")

for _pkg in ("shared", "collector", "api", "exporter"):
    _p = os.path.join(_LAMBDA, _pkg)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Routes live inside lambda/api/routes/ but import siblings from lambda/api/
_routes = os.path.join(_LAMBDA, "api", "routes")
if _routes not in sys.path:
    sys.path.insert(0, _routes)


# ── Handler alias helpers (avoids name conflict across handler.py files) ──────

def _load_as(module_name: str, file_path: str):
    """Load a file as a named module; cache in sys.modules to avoid re-loading."""
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[module_name]


collector_handler = _load_as(
    "collector_handler", os.path.join(_LAMBDA, "collector", "handler.py")
)
api_handler = _load_as(
    "api_handler", os.path.join(_LAMBDA, "api", "handler.py")
)
exporter_handler = _load_as(
    "exporter_handler", os.path.join(_LAMBDA, "exporter", "handler.py")
)
