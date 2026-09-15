"""CDP JWT shape tests with ephemeral ES256 key — no Coinbase calls."""

from __future__ import annotations

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from tayder.execute.jwt_auth import build_jwt


def _pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_build_jwt_claims():
    pem = _pem()
    name = "organizations/test/apiKeys/abc"
    token = build_jwt(name, pem, "GET", "/api/v3/brokerage/accounts")
    # decode without verify to inspect claims (need key for verify)
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    pub = key.public_key()
    claims = jwt.decode(
        token,
        pub,
        algorithms=["ES256"],
        options={"require": ["sub", "iss", "nbf", "exp", "uri"]},
    )
    assert claims["sub"] == name
    assert claims["iss"] == "cdp"
    assert claims["uri"] == "GET api.coinbase.com/api/v3/brokerage/accounts"
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == name
    assert "nonce" in header
