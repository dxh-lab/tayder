"""Live Coinbase Advanced Trade REST execution (CDP JWT)."""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from tayder.config import Settings
from tayder.execute.jwt_auth import auth_headers
from tayder.models import Fill, Proposal, Side


class Executor(Protocol):
    def execute(self, proposal: Proposal, stake_usd: float) -> Fill: ...


class LiveCoinbaseExecutor:
    """Market IOC-style market order via Advanced Trade API."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=20.0)
        parsed = urlparse(settings.coinbase_api_base)
        self._host = parsed.netloc or "api.coinbase.com"
        self._base = settings.coinbase_api_base.rstrip("/")

    def close(self) -> None:
        self._client.close()

    def execute(self, proposal: Proposal, stake_usd: float) -> Fill:
        path = "/api/v3/brokerage/orders"
        method = "POST"
        pem = self._settings.private_key_pem()
        headers = auth_headers(
            self._settings.coinbase_api_key_name,
            pem,
            method,
            path,
            host=self._host,
        )
        client_oid = str(uuid4())
        body: dict = {
            "client_order_id": client_oid,
            "product_id": proposal.product_id,
            "side": proposal.side.value,
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": f"{stake_usd:.2f}"
                    if proposal.side == Side.BUY
                    else None,
                    "base_size": None,
                }
            },
        }
        # For SELL, market IOC wants base_size; without inventory size we use quote for BUY only.
        # Spot helper: SELL path expects meta['base_size'] when available.
        ioc = body["order_configuration"]["market_market_ioc"]
        if proposal.side == Side.SELL:
            base = proposal.meta.get("base_size")
            if not base:
                raise ValueError("sell_requires_base_size")
            ioc.clear()
            ioc["base_size"] = f"{float(base):.8f}"
        else:
            ioc.pop("base_size", None)
            ioc["quote_size"] = f"{stake_usd:.2f}"

        url = f"{self._base}{path}"
        r = self._client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        success = data.get("success", False)
        order_id = (
            (data.get("success_response") or {}).get("order_id")
            or data.get("order_id")
            or client_oid
        )
        if not success and "error_response" in data:
            raise RuntimeError(str(data["error_response"]))
        # Fill details may require a follow-up GET; record best-effort.
        price = float(proposal.signal_price)
        size = stake_usd / price if price else 0.0
        fee = stake_usd * (self._settings.taker_fee_bps / 10_000)
        return Fill(
            proposal_id=proposal.proposal_id,
            product_id=proposal.product_id,
            side=proposal.side,
            price=price,
            size=size,
            fee_usd=fee,
            notional_usd=stake_usd,
            mode="live",
            order_id=str(order_id),
        )


class MissingCredentialsExecutor:
    """Stub when LIVE is set but credentials are absent."""

    def execute(self, proposal: Proposal, stake_usd: float) -> Fill:
        raise RuntimeError("live_credentials_missing")
