from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tayder.config import Settings
from tayder.models import Candle
from tayder.replay import replay, ReplayMarket


def data():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    prices = [100.] * 22 + [95.] * 5 + [105.] * 5 + [100.] * 4
    bars = [Candle(start + timedelta(minutes=15*i), p, p, p, p, 1) for i, p in enumerate(prices)]
    return {"BTC-USD": bars, "ETH-USD": list(bars)}


def settings(**kwargs):
    return replace(Settings(bankroll_usd=10, enforce_fee_dominance=False), **kwargs)


def test_replay_shared_cash_real_gates_and_determinism():
    first = replay(data(), settings=settings())
    assert first == replay(data(), settings=settings())
    assert first["fills"]
    assert all(f["product_id"] == "BTC-USD" for f in first["fills"])
    assert any(d["reason"] == "reject:max_open_positions" for s in first["scans"] for d in s["decisions"].values())
    assert all(f["filled_at"].startswith("2025-") for f in first["fills"])
    assert first["fees_usd"] > 0


def test_expiry_and_same_candle_dedupe():
    r = replay(data(), settings=settings(), approval_latency_seconds=301)
    assert not r["fills"]
    assert any(p["status"] == "expired" for p in r["proposals"])
    keys = [(p["product_id"], p["meta"]["signal_candle_at"]) for p in r["proposals"]]
    assert len(keys) == len(set(keys))


def test_no_current_bar_close_leakage():
    d = data()
    now = d["BTC-USD"][22].ts
    m = ReplayMarket(d, lambda: now, 4)
    m.index = 22
    assert len(m.candles("BTC-USD")) == 22
    assert m.top_of_book("BTC-USD").mid == 95
    assert m.candles("BTC-USD")[-1].close == 100


def test_offline_filters_and_missing_jev_are_disclosed():
    base = replay(data(), settings=settings())
    simple = replay(data(), settings=settings(), policy="simple_trend")
    jev = replay(data(), settings=settings(), policy="jev", observations={})
    assert simple["filtered_buys"] and jev["missing_observations"]
    assert not jev["fills"] and base["fills"]


def test_coverage_validation():
    d = data()
    d["ETH-USD"] = d["ETH-USD"][:-1]
    with pytest.raises(ValueError, match="identical"):
        replay(d)


def test_jev_uses_availability_at_approval_not_signal_time():
    base = replay(data(), settings=settings())
    proposal = next(p for p in base['proposals'] if p['side'] == 'BUY')
    key = (proposal['product_id'], proposal['meta']['signal_candle_at'])
    created = datetime.fromisoformat(proposal['created_at'])
    item = {'available_at': (created + timedelta(seconds=1)).isoformat(),
            'answer': {'choice': 'range_bound', 'confidence': .9}}
    observed = replay(data(), settings=settings(), policy='jev', observations={key: item})
    assert observed['fills']
    item['available_at'] = (created + timedelta(days=1)).isoformat()
    late = replay(data(), settings=settings(), policy='jev', observations={key: item})
    assert not late['fills'] and late['missing_observations']
