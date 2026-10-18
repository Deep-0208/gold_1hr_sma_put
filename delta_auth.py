"""
═════════════════════════════════════════════════════════════
  DELTA_AUTH — HMAC-SHA256 Authentication for Delta Exchange
═════════════════════════════════════════════════════════════

Signature formula (per Delta docs):
    payload   = METHOD + TIMESTAMP + PATH + ?QUERY_STRING + BODY
    signature = HMAC-SHA256(payload, api_secret).hexdigest()

Headers: api-key, timestamp (epoch seconds), signature.
Standard library only.
"""

import hashlib
import hmac
import time
from typing import Dict, Optional


def generate_signature(
    api_secret: str,
    method: str,
    path: str,
    timestamp: str,
    query_string: str = "",
    body: str = "",
) -> str:
    """Hex HMAC-SHA256 signature for one Delta Exchange request."""
    if query_string and not query_string.startswith("?"):
        query_string = "?" + query_string
    payload = method + timestamp + path + query_string + body
    return hmac.new(
        api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def get_auth_headers(
    api_key: str,
    api_secret: str,
    method: str,
    path: str,
    query_string: str = "",
    body: str = "",
    timestamp: Optional[str] = None,
) -> Dict[str, str]:
    """Signed headers for an authenticated Delta Exchange request."""
    if timestamp is None:
        timestamp = str(int(time.time()))

    return {
        "api-key": api_key,
        "timestamp": timestamp,
        "signature": generate_signature(
            api_secret=api_secret,
            method=method.upper(),
            path=path,
            timestamp=timestamp,
            query_string=query_string,
            body=body,
        ),
        "Content-Type": "application/json",
        "User-Agent": "Gold1HrSMA/1.0",
    }
