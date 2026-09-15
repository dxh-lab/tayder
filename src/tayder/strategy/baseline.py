"""Simple 15m mean-reversion baseline → Proposal or None."""

from __future__ import annotations

from tayder.models import Candle, Proposal, Side


def mean_reversion_signal(
    product_id: str,
    candles: list[Candle],
    *,
    lookback: int = 20,
    z_entry: float = 1.5,
    notional_usd: float = 5.0,
) -> Proposal | None:
    """
    Fade moves when close is ≥ z_entry stdevs from SMA(lookback).
    BUY when oversold (z ≤ -z_entry); SELL when overbought (z ≥ z_entry).
    Spot helper only — SELL implies closing / reducing; worker gates inventory.
    """
    if len(candles) < lookback + 1:
        return None
    window = candles[-(lookback + 1) : -1]  # exclude forming candle
    closes = [c.close for c in window]
    sma = sum(closes) / len(closes)
    var = sum((x - sma) ** 2 for x in closes) / len(closes)
    std = var**0.5
    if std <= 0:
        return None
    last = candles[-2]  # last closed
    z = (last.close - sma) / std
    if z <= -z_entry:
        return Proposal(
            product_id=product_id,
            side=Side.BUY,
            notional_usd=notional_usd,
            reason=f"mean_reversion_buy z={z:.2f} sma={sma:.2f}",
            signal_price=last.close,
            meta={"z": z, "sma": sma, "lookback": lookback},
        )
    if z >= z_entry:
        return Proposal(
            product_id=product_id,
            side=Side.SELL,
            notional_usd=notional_usd,
            reason=f"mean_reversion_sell z={z:.2f} sma={sma:.2f}",
            signal_price=last.close,
            meta={"z": z, "sma": sma, "lookback": lookback},
        )
    return None
