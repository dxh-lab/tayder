"""Coinbase CDP ES256 JWT (URI-bound, short-lived)."""

from __future__ import annotations

import secrets
import re
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def validate_api_base(base: str) -> str:
    """Accept only Coinbase's exact HTTPS origin (optional single trailing slash)."""
    if base not in ("https://api.coinbase.com", "https://api.coinbase.com/"):
        raise ValueError("live_requires_https_api_coinbase_com")
    return "https://api.coinbase.com"


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
    if host != "api.coinbase.com":
        raise ValueError("invalid_coinbase_jwt_host")
    if method.upper() not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
        raise ValueError("invalid_coinbase_jwt_method")
    # Query parameters are passed separately and are not part of the signed URI,
    # matching Coinbase's official Advanced Trade Python REST client.
    if (not re.fullmatch(r"/api/v3/brokerage/[A-Za-z0-9/_-]+", path)
            or "//" in path):
        raise ValueError("invalid_coinbase_jwt_path")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120:
        raise ValueError("invalid_coinbase_jwt_ttl")
    if not api_key_name or not api_key_name.strip() or not private_key_pem:
        raise ValueError("live_credentials_missing")
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
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("coinbase_requires_es256_p256_key")
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
