"""Coinbase CDP ES256 JWT (URI-bound, short-lived)."""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization


def build_jwt(
    api_key_name: str,
    private_key_pem: str,
    method: str,
    path: str,
    *,
    host: str = "api.coinbase.com",
    ttl_seconds: int = 120,
) -> str:
    """
    Build a CDP JWT for Advanced Trade REST.

    Claims follow Coinbase CDP secret-key auth:
      sub = api key name
      iss = "cdp"
      uri = "{METHOD} {host}{path}"
      nbf / exp short window
      nonce = random hex
    """
    now = int(time.time())
    uri = f"{method.upper()} {host}{path}"
    payload: dict[str, Any] = {
        "sub": api_key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + ttl_seconds,
        "uri": uri,
    }
    headers = {
        "kid": api_key_name,
        "nonce": secrets.token_hex(16),
        "typ": "JWT",
        "alg": "ES256",
    }
    key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    token = jwt.encode(payload, key, algorithm="ES256", headers=headers)
    return token if isinstance(token, str) else token.decode("utf-8")


def auth_headers(
    api_key_name: str,
    private_key_pem: str,
    method: str,
    path: str,
    *,
    host: str = "api.coinbase.com",
) -> dict[str, str]:
    token = build_jwt(api_key_name, private_key_pem, method, path, host=host)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
