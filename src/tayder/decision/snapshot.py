"""Public, point-in-time features. No balances, credentials, or Discord content."""
import math
from datetime import timedelta

QUESTION_VERSION = "market-context-v1"
OPTIONS = {
    "range_bound": "Recent prices oscillate without a sustained directional trend.",
    "uptrend": "Recent prices show a sustained upward direction.",
    "downtrend": "Recent prices show a sustained downward direction.",
    "unclear": "Evidence is insufficient, conflicting, or does not fit the other options.",
}


def make_request(proposal, candles, book, model):
    at = proposal.created_at
    closed = [c for c in candles if c.ts + timedelta(seconds=900) <= at][-50:]
    if len(closed) < 2 or any(b.ts - a.ts != timedelta(seconds=900) for a, b in zip(closed, closed[1:])):
        raise ValueError("insufficient_contiguous_history")
    closes = [c.close for c in closed]
    if any(not math.isfinite(c) or c <= 0 for c in closes):
        raise ValueError("invalid_prices")
    returns = [b / a - 1 for a, b in zip(closes, closes[1:])]
    mean = sum(returns) / len(returns)
    volatility = (sum((r - mean) ** 2 for r in returns) / len(returns)) ** .5
    state = {
        "question_version": QUESTION_VERSION, "product_id": proposal.product_id,
        "observed_at": at.isoformat(), "quote_at": book.ts.isoformat(),
        "last_closed_candle_at": closed[-1].ts.isoformat(),
        "closes": closes, "return_bps": (closes[-1] / closes[0] - 1) * 10000,
        "volatility_bps": volatility * 10000, "spread_bps": book.spread_bps,
        "z": proposal.meta.get("z"), "sma": proposal.meta.get("sma"),
    }
    return {"model": model, "state": state, "questions": {"regime": {
        "type": "choice", "instructions": "Classify only the supplied recent market behavior. Do not forecast returns or recommend trades.",
        "criteria": dict(OPTIONS),
    }}}
