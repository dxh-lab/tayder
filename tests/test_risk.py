"""Risk gate unit tests — no network, no keys."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tayder.config import Settings
from tayder.models import Proposal, Side
from tayder.risk.gates import RiskState, check_proposal


def _settings(**kwargs) -> Settings:
    base = dict(
        mode="paper",
        bankroll_usd=10.0,
        max_open_positions=1,
        min_notional_usd=1.0,
        cooldown_seconds=300,
        daily_loss_stop_pct=0.25,
        fee_dominance_bps=20.0,
        taker_fee_bps=60.0,
        killed=False,
    )
    base.update(kwargs)
    return Settings(**base)


def _prop(**kwargs) -> Proposal:
    d = dict(
        product_id="BTC-USD",
        side=Side.BUY,
        notional_usd=5.0,
        reason="t",
        signal_price=50_000.0,
    )
    d.update(kwargs)
    return Proposal(**d)


def test_ok_basic():
    d = check_proposal(_prop(), _settings(), RiskState(), expected_edge_bps=300)
    assert d.ok
    assert d.stake_usd == 5.0


def test_stake_capped_to_bankroll():
    d = check_proposal(_prop(notional_usd=50), _settings(bankroll_usd=10), RiskState(), expected_edge_bps=300)
    assert d.ok
    assert d.stake_usd * 1.006 == pytest.approx(10.0)


def test_bankroll_over_mode_cap_refused():
    d = check_proposal(_prop(), _settings(mode="paper", bankroll_usd=101), RiskState())
    assert not d.ok
    assert d.reason == "bankroll_out_of_bounds"
    d = check_proposal(_prop(), _settings(mode="live", bankroll_usd=11), RiskState())
    assert not d.ok
    assert d.reason == "bankroll_out_of_bounds"


def test_paper_allows_100_bankroll_live_still_caps_at_10():
    assert check_proposal(
        _prop(notional_usd=50),
        _settings(mode="paper", bankroll_usd=100),
        RiskState(cash_usd=100),
        expected_edge_bps=300,
    ).ok
    d = check_proposal(
        _prop(),
        _settings(mode="live", bankroll_usd=100, coinbase_api_key_name="k",
                  coinbase_api_private_key="p", discord_allowlist=frozenset({1})),
        RiskState(),
        expected_edge_bps=300,
    )
    assert not d.ok and d.reason == "bankroll_out_of_bounds"


def test_below_min_notional():
    d = check_proposal(
        _prop(notional_usd=0.5),
        _settings(min_notional_usd=1.0, bankroll_usd=10),
        RiskState(cash_usd=0.5),
    )
    assert not d.ok
    assert d.reason == "below_min_notional"


def test_max_one_open_blocks_buy():
    d = check_proposal(
        _prop(side=Side.BUY),
        _settings(),
        RiskState(open_positions=1),
    )
    assert not d.ok
    assert d.reason == "max_open_positions"


def test_max_open_allows_sell():
    d = check_proposal(
        _prop(side=Side.SELL),
        _settings(),
        RiskState(open_positions=1, holdings={"BTC-USD": 0.0001}),
    )
    assert d.ok


def test_cooldown():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    state = RiskState(last_trade_at=now - timedelta(seconds=60))
    d = check_proposal(_prop(), _settings(cooldown_seconds=300), state, now=now)
    assert not d.ok
    assert d.reason == "cooldown"


def test_daily_loss_stop():
    # 25% of $10 = $2.5 → stop at -2.5
    state = RiskState(day_realized_pnl=-2.5, day_key="2026-01-01")
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    d = check_proposal(_prop(), _settings(daily_loss_stop_pct=0.25), state, now=now)
    assert not d.ok
    assert d.reason == "daily_loss_stop"


def test_fee_dominance():
    # RT fees = 120 bps; edge 50 + buffer 20 → dominated
    d = check_proposal(
        _prop(),
        _settings(taker_fee_bps=60, fee_dominance_bps=20, enforce_fee_dominance=True),
        RiskState(),
        expected_edge_bps=50,
    )
    assert not d.ok
    assert d.reason == "fee_dominance"
    assert d.cost_warning
    assert d.required_edge_bps == pytest.approx(160)


def test_fee_advisory_allows_proposal_with_warning():
    d = check_proposal(
        _prop(),
        _settings(taker_fee_bps=60, fee_dominance_bps=20, enforce_fee_dominance=False),
        RiskState(),
        expected_edge_bps=50,
    )
    assert d.ok
    assert d.cost_warning
    assert d.required_edge_bps == pytest.approx(160)


def test_fee_ok_when_edge_large():
    d = check_proposal(
        _prop(),
        _settings(taker_fee_bps=60, fee_dominance_bps=20),
        RiskState(),
        expected_edge_bps=200,
    )
    assert d.ok
    assert not d.cost_warning


def test_kill_switch():
    d = check_proposal(_prop(), _settings(killed=True), RiskState())
    assert not d.ok
    assert d.reason == "kill_switch"


def test_live_missing_creds():
    d = check_proposal(
        _prop(),
        _settings(mode="live", coinbase_api_key_name="", coinbase_api_private_key=""),
        RiskState(),
    )
    assert not d.ok
    assert d.reason == "live_credentials_missing"


def test_missing_or_nonfinite_edge_never_bypasses_buy_cost_gate():
    for edge in (None, float('nan'), float('inf')):
        d = check_proposal(_prop(), _settings(), RiskState(), expected_edge_bps=edge)
        assert not d.ok and d.reason == 'edge_unknown'


def test_pending_cash_and_position_reservations_are_enforced():
    d = check_proposal(_prop(), _settings(), RiskState(cash_usd=5, reserved_cash=5), expected_edge_bps=500)
    assert not d.ok and d.reason == 'below_min_notional'
    d = check_proposal(_prop(), _settings(), RiskState(reserved_positions=1), expected_edge_bps=500)
    assert not d.ok and d.reason == 'max_open_positions'


def test_day_rollover_clears_only_current_day_gate():
    state = RiskState(day_realized_pnl=-5, day_key='2026-01-01')
    decision = check_proposal(_prop(), _settings(), state, expected_edge_bps=500,
                             now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert decision.ok
    assert state.day_key == '2026-01-02' and state.day_realized_pnl == 0
