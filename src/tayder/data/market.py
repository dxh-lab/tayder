"""Coinbase public market data (candles + top-of-book)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

import httpx

from tayder.models import Candle, TopOfBook

PUBLIC_BASE = "https://api.coinbase.com/api/v3/brokerage/market"


class MarketData(Protocol):
    def candles(
        self, product_id: str, granularity: int, limit: int = 100
    ) -> list[Candle]: ...

    def top_of_book(self, product_id: str) -> TopOfBook: ...


class CoinbasePublicMarket:
    """httpx client for Coinbase Advanced Trade public market endpoints."""

    def __init__(self, base: str = PUBLIC_BASE, timeout: float = 15.0) -> None:
        self._base = base.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def candles(
        self, product_id: str, granularity: int = 900, limit: int = 100
    ) -> list[Candle]:
        # granularity in seconds → Coinbase candle enum
        gran_map = {
            60: "ONE_MINUTE",
            300: "FIVE_MINUTE",
            900: "FIFTEEN_MINUTE",
            3600: "ONE_HOUR",
            21600: "SIX_HOUR",
            86400: "ONE_DAY",
        }
        gran = gran_map.get(granularity, "FIFTEEN_MINUTE")
        url = f"{self._base}/products/{product_id}/candles"
        r = self._client.get(url, params={"granularity": gran, "limit": limit})
        r.raise_for_status()
        data = r.json()
        raw = data.get("candles") or data.get("data") or []
        out: list[Candle] = []
        for c in raw:
            # public API may return list [start, low, high, open, close, volume]
            # or dict {start, low, high, open, close, volume}
            if isinstance(c, dict):
                start = int(c["start"])
                out.append(
                    Candle(
                        ts=datetime.fromtimestamp(start, tz=timezone.utc),
                        open=float(c["open"]),
                        high=float(c["high"]),
                        low=float(c["low"]),
                        close=float(c["close"]),
                        volume=float(c["volume"]),
                    )
                )
            else:
                start, low, high, open_, close, volume = c[:6]
                out.append(
                    Candle(
                        ts=datetime.fromtimestamp(int(start), tz=timezone.utc),
                        open=float(open_),
                        high=float(high),
                        low=float(low),
                        close=float(close),
                        volume=float(volume),
                    )
                )
        out.sort(key=lambda x: x.ts)
        return out[-limit:]

    def top_of_book(self, product_id: str) -> TopOfBook:
        url = f"{self._base}/product_book"
        r = self._client.get(url, params={"product_id": product_id, "limit": 1})
        r.raise_for_status()
        book = r.json().get("pricebook") or r.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            # fallback ticker
            return self._ticker_fallback(product_id)
        bid = bids[0]
        ask = asks[0]
        return TopOfBook(
            product_id=product_id,
            bid=float(bid["price"] if isinstance(bid, dict) else bid[0]),
            ask=float(ask["price"] if isinstance(ask, dict) else ask[0]),
            bid_size=float(bid["size"] if isinstance(bid, dict) else bid[1]),
            ask_size=float(ask["size"] if isinstance(ask, dict) else ask[1]),
        )

    def _ticker_fallback(self, product_id: str) -> TopOfBook:
        url = f"{self._base}/products/{product_id}/ticker"
        r = self._client.get(url)
        r.raise_for_status()
        t = r.json()
        price = float(t.get("price") or t.get("trade_id") or 0)
        # best-effort if only mid available
        bid = float(t.get("best_bid") or price)
        ask = float(t.get("best_ask") or price)
        return TopOfBook(
            product_id=product_id,
            bid=bid,
            ask=ask,
            bid_size=float(t.get("best_bid_size") or 0),
            ask_size=float(t.get("best_ask_size") or 0),
        )
