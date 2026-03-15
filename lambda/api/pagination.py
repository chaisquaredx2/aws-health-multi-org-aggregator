"""
pagination.py — DynamoDB cursor encode/decode for API pagination tokens.
"""

import base64
import json


def encode_token(last_evaluated_key: dict) -> str:
    """Encode a DynamoDB LastEvaluatedKey as a base64 string."""
    return base64.b64encode(json.dumps(last_evaluated_key).encode()).decode()


def decode_token(token: str) -> dict:
    """Decode a base64 pagination token back to a DynamoDB ExclusiveStartKey."""
    return json.loads(base64.b64decode(token).decode())
