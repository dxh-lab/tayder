"""Simple 15m mean-reversion baseline → Proposal or None."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from tayder.models import Candle, Proposal, Side, utcnow


def mean_reversion_signal(
    product_id: str,
    candles: list[Candle],
    *,
    lookback: int = 20,
    z_entry: float = 1.5,
    notional_usd: float = 5.0,
    now: datetime | None = None,
    granularity_seconds: int = 900,
) -> Proposal | None:
    """
    Fade moves when close is ≥ z_entry stdevs from SMA(lookback).
    BUY when oversold (z ≤ -z_entry); SELL when overbought (z ≥ z_entry).
    Candle timestamps denote interval starts. With ``now``, only intervals ending
    at or before it are used, so input may include forming or future candles.
    Without ``now``, callers promise the input contains only completed candles.
    Spot SELL implies reducing existing inventory.

    ``estimated_edge_bps`` (also ``edge_bps`` for existing callers) is the gross
    distance from the last completed close to its moving mean. It is an
    unvalidated hypothesis, not a calibrated expected return or profit promise.
    """
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 2:
        raise ValueError("lookback must be an integer >= 2")
    if not math.isfinite(z_entry) or z_entry <= 0:
        raise ValueError("z_entry must be finite and positive")
    if not math.isfinite(notional_usd) or notional_usd <= 0:
        raise ValueError("notional_usd must be finite and positive")
    if (isinstance(granularity_seconds, bool)
            or not isinstance(granularity_seconds, int) or granularity_seconds <= 0):
        raise ValueError("granularity_seconds must be a positive integer")
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("now must be timezone-aware")
    interval = timedelta(seconds=granularity_seconds)
    for candle in candles:
        if candle.ts.tzinfo is None or candle.ts.utcoffset() is None:
            raise ValueError("candle timestamps must be timezone-aware")
    completed = sorted((c for c in candles if now is None or c.ts + interval <= now), key=lambda c: c.ts)
    if len(completed) < lookback:
        return None
    window = completed[-lookback:]
    if any(b.ts - a.ts != interval for a, b in zip(window, window[1:])):
        return None  # Do not mistake missing/duplicate intervals for a regular SMA.
    closes = [c.close for c in window]
    if any(not math.isfinite(price) or price <= 0 for price in closes):
        raise ValueError("completed close prices must be finite and positive")
    sma = sum(closes) / len(closes)
    var = sum((x - sma) ** 2 for x in closes) / len(closes)
    std = var**0.5
    if std <= 0:
        return None
    last = window[-1]
    z = (last.close - sma) / std
    side = Side.BUY if z <= -z_entry else Side.SELL if z >= z_entry else None
    if side is None:
        return None
    edge_bps = abs(sma - last.close) / last.close * 10_000
    return Proposal(
        product_id=product_id,
        side=side,
        notional_usd=notional_usd,
        reason=f"mean_reversion_{side.value.lower()} z={z:.2f} sma={sma:.2f}",
        signal_price=last.close,
        created_at=now or utcnow(),
        meta={
            "z": z, "sma": sma, "lookback": lookback,
            "direction": side.value,
            "estimated_edge_bps": edge_bps, "edge_bps": edge_bps,
            "edge_basis": "gross_distance_to_mean",
            "validation_status": "unvalidated_hypothesis",
            "signal_candle_at": last.ts.isoformat(),
            "signal_available_at": (last.ts + interval).isoformat(),
        },
    )
