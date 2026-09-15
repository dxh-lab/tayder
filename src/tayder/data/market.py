"""Unauthenticated Coinbase Advanced Trade market data (public GETs only).

Contracts: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-candles
and https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-book
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Protocol

import httpx

from tayder.models import Candle, TopOfBook

PUBLIC_BASE = "https://api.coinbase.com/api/v3/brokerage/market"
GRANULARITIES = {
    60: "ONE_MINUTE", 300: "FIVE_MINUTE", 900: "FIFTEEN_MINUTE",
    1800: "THIRTY_MINUTE", 3600: "ONE_HOUR", 7200: "TWO_HOUR",
    14400: "FOUR_HOUR", 21600: "SIX_HOUR", 86400: "ONE_DAY",
}
MAX_CANDLES = 350
Timestamp = datetime | int | float


class MarketDataError(ValueError):
    """Missing, malformed, inconsistent, or unsafe market data."""


def _number(value: object, name: str, *, allow_zero: bool = False) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError(f"Invalid {name}") from exc
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        raise MarketDataError(f"Invalid {name}: must be finite and positive"
                              + (" or zero" if allow_zero else ""))
    return result


def _seconds(value: Timestamp, name: str) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be an aware datetime or UNIX seconds")
    result = _number(value, name, allow_zero=True)
    try:
        datetime.fromtimestamp(result, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"{name} is outside the supported timestamp range") from exc
    return result


def _product(product_id: str) -> None:
    if not isinstance(product_id, str) or not re.fullmatch(
        r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", product_id
    ):
        raise ValueError("product_id must be a trading pair such as BTC-USD")


def _granularity(granularity: int) -> str:
    if type(granularity) is not int or granularity not in GRANULARITIES:
        raise ValueError(f"Unsupported candle granularity: {granularity!r}")
    return GRANULARITIES[granularity]


class MarketData(Protocol):
    def candles(
        self, product_id: str, granularity: int, limit: int = 100
    ) -> list[Candle]: ...

    def top_of_book(self, product_id: str) -> TopOfBook: ...


class CoinbasePublicMarket:
    """Validate exchange timestamps and fail closed without ticker fallback.

    Defaults allow 30 seconds of quote age, 5 seconds of future clock skew,
    and 1000 basis points (10%) spread. Strategy limits may be stricter.
    Injected httpx clients remain caller-owned. HTTP errors propagate.
    """

    def __init__(
        self, base: str = PUBLIC_BASE, timeout: float = 15.0, *,
        client: httpx.Client | None = None,
        max_age_seconds: float = 30.0,
        max_spread_bps: float = 1000.0,
        future_tolerance_seconds: float = 5.0,
    ) -> None:
        self._max_age = _number(max_age_seconds, "max_age_seconds")
        self._max_spread = _number(max_spread_bps, "max_spread_bps")
        self._future_tolerance = _number(
            future_tolerance_seconds, "future_tolerance_seconds", allow_zero=True
        )
        self._base = base.rstrip("/")
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CoinbasePublicMarket:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get(self, path: str, params: dict) -> dict:
        response = self._client.get(
            f"{self._base}/{path}", params=params,
            headers={"Cache-Control": "no-cache"},
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise MarketDataError("Market response is not JSON") from exc
        if not isinstance(data, dict):
            raise MarketDataError("Market response must be an object")
        return data

    def candles(
        self, product_id: str, granularity: int = 900, limit: int = 100,
        *, now: Timestamp | None = None,
    ) -> list[Candle]:
        """Return closed candles from the latest limit-bucket window.

        Missing buckets remain gaps. ``now`` is captured once and can be
        supplied for reproducibility. Limits above 350 are paginated.
        """
        _product(product_id)
        _granularity(granularity)
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        current = _seconds(datetime.now(timezone.utc) if now is None else now, "now")
        end = math.floor(current / granularity) * granularity
        start = max(0, end - limit * granularity)
        if start == end:
            return []
        return self.candles_history(product_id, granularity, start, end, now=current)

    def candles_history(
        self, product_id: str, granularity: int, start: Timestamp, end: Timestamp,
        *, now: Timestamp | None = None,
    ) -> list[Candle]:
        """Return sorted unique closed candles fully contained in [start, end).

        Bounds accept aware datetimes or UNIX seconds. Partial boundary buckets
        and the bucket forming at ``now`` are excluded. Pagination advances by
        time even for empty pages. Identical duplicates collapse; conflicting
        duplicates raise. No CSV or CLI side effects are performed.
        """
        _product(product_id)
        gran = _granularity(granularity)
        lower, upper = _seconds(start, "start"), _seconds(end, "end")
        if lower >= upper:
            raise ValueError("start must be before end")
        current = _seconds(datetime.now(timezone.utc) if now is None else now, "now")
        cursor = math.ceil(lower / granularity) * granularity
        stop = math.floor(min(upper, current) / granularity) * granularity
        candles: dict[datetime, Candle] = {}
        while cursor < stop:
            page_end = min(stop, cursor + MAX_CANDLES * granularity)
            data = self._get(f"products/{product_id}/candles", {
                "start": str(cursor),
                # Avoid an inclusive API end selecting a 351st boundary bucket.
                "end": str(page_end - 1),
                "granularity": gran,
                "limit": (page_end - cursor) // granularity,
            })
            raw = data.get("candles")
            if not isinstance(raw, list):
                raise MarketDataError("Missing or invalid candles array")
            for row in raw:
                candle = self._candle(row)
                timestamp = candle.ts.timestamp()
                if timestamp % granularity:
                    raise MarketDataError("Candle timestamp is not aligned to granularity")
                if not (cursor <= timestamp and timestamp + granularity <= page_end):
                    continue
                previous = candles.get(candle.ts)
                if previous is not None and previous != candle:
                    raise MarketDataError("Conflicting candles for the same timestamp")
                candles[candle.ts] = candle
            cursor = page_end
        return sorted(candles.values(), key=lambda candle: candle.ts)

    @staticmethod
    def _candle(row: object) -> Candle:
        if not isinstance(row, dict):
            raise MarketDataError("Candle must be an Advanced Trade candle object")
        start = _number(row.get("start"), "candle start", allow_zero=True)
        if not start.is_integer():
            raise MarketDataError("Candle start must be integer UNIX seconds")
        timestamp = _seconds(start, "candle start")
        values = {key: _number(row.get(key), f"candle {key}")
                  for key in ("open", "high", "low", "close")}
        if not (values["low"] <= min(values["open"], values["close"])
                <= max(values["open"], values["close"]) <= values["high"]):
            raise MarketDataError("Inconsistent candle OHLC prices")
        return Candle(
            ts=datetime.fromtimestamp(timestamp, timezone.utc), **values,
            volume=_number(row.get("volume"), "candle volume", allow_zero=True),
        )

    def top_of_book(
        self, product_id: str, *, now: Timestamp | None = None
    ) -> TopOfBook:
        """Return a validated quote stamped with Coinbase ``pricebook.time``."""
        _product(product_id)
        reference = None if now is None else _seconds(now, "now")
        data = self._get("product_book", {"product_id": product_id, "limit": 1})
        book = data.get("pricebook")
        if not isinstance(book, dict) or book.get("product_id") != product_id:
            raise MarketDataError("Missing pricebook or mismatched product_id")
        try:
            timestamp = datetime.fromisoformat(book["time"].replace("Z", "+00:00"))
            timestamp = datetime.fromtimestamp(_seconds(timestamp, "book time"), timezone.utc)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise MarketDataError("Missing or invalid server book timestamp") from exc
        # Measure after receipt to include transport latency, but preserve server time.
        current = datetime.now(timezone.utc).timestamp() if reference is None else reference
        age = current - timestamp.timestamp()
        if age > self._max_age:
            raise MarketDataError(f"Stale order book ({age:.3f} seconds old)")
        if age < -self._future_tolerance:
            raise MarketDataError("Order book timestamp is in the future")
        levels = {}
        for side in ("bids", "asks"):
            rows = book.get(side)
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                raise MarketDataError(f"Missing or invalid {side}")
            levels[side] = (
                _number(rows[0].get("price"), f"{side} price"),
                _number(rows[0].get("size"), f"{side} size"),
            )
        result = TopOfBook(
            product_id=product_id, bid=levels["bids"][0], ask=levels["asks"][0],
            bid_size=levels["bids"][1], ask_size=levels["asks"][1], ts=timestamp,
        )
        if result.bid >= result.ask:
            raise MarketDataError("Locked or crossed order book")
        if not math.isfinite(result.mid) or not math.isfinite(result.spread_bps):
            raise MarketDataError("Non-finite order book mid or spread")
        if result.spread_bps > self._max_spread:
            raise MarketDataError("Order book spread exceeds sanity limit")
        return result
