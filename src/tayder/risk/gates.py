"""Preflight and final execution checks, including pending capital reservations."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tayder.config import Settings
from tayder.models import Proposal, Side


@dataclass
class RiskState:
    open_positions: int = 0
    last_trade_at: datetime | None = None
    day_realized_pnl: float = 0.0
    day_key: str = ""
    cash_usd: float | None = None
    holdings: dict[str, float] = field(default_factory=dict)
    reserved_cash: float = 0.0
    reserved_positions: int = 0


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: str
    stake_usd: float = 0.0
    required_edge_bps: float | None = None
    cost_warning: bool = False


def required_edge_bps(settings: Settings, spread_bps: float = 0.0) -> float:
    """Round-trip taker fees + buffer + full spread + two-sided slippage."""
    return (2 * settings.taker_fee_bps + settings.fee_dominance_bps
            + spread_bps + 2 * settings.slippage_bps)


def refresh_day(state: RiskState, now: datetime | None = None) -> None:
    key = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    if state.day_key != key:
        state.day_key = key
        state.day_realized_pnl = 0.0


def check_proposal(proposal: Proposal, settings: Settings, state: RiskState, *,
                   now: datetime | None = None, expected_edge_bps: float | None = None,
                   spread_bps: float = 0.0) -> RiskDecision:
    now = now or datetime.now(timezone.utc)
    refresh_day(state, now)
    if settings.killed:
        return RiskDecision(False, "kill_switch")
    if proposal.is_expired(now):
        return RiskDecision(False, "expired")
    if proposal.side not in (Side.BUY, Side.SELL):
        return RiskDecision(False, "invalid_side")
    if proposal.product_id not in settings.strategy_pairs:
        return RiskDecision(False, "invalid_product")
    if not math.isfinite(settings.bankroll_usd) or not 0 < settings.bankroll_usd <= 10:
        return RiskDecision(False, "bankroll_out_of_bounds")
    if not all(math.isfinite(v) and v > 0 for v in (proposal.notional_usd, proposal.signal_price)):
        return RiskDecision(False, "invalid_amount")
    if settings.is_live:
        if not settings.coinbase_api_key_name or not settings.private_key_pem():
            return RiskDecision(False, "live_credentials_missing")
        if not settings.discord_allowlist:
            return RiskDecision(False, "live_allowlist_missing")
    if proposal.side == Side.SELL:
        size = state.holdings.get(proposal.product_id, 0.0)
        if not math.isfinite(size) or size <= 0:
            return RiskDecision(False, "no_inventory")
        stake = min(proposal.notional_usd, size * proposal.signal_price)
        if stake < settings.min_notional_usd:
            return RiskDecision(False, "below_min_notional", stake)
        # Risk-reducing exits do not need cash and remain possible after a loss stop.
        return RiskDecision(True, "ok", stake)

    cash = state.cash_usd if state.cash_usd is not None else settings.bankroll_usd
    if not math.isfinite(cash):
        return RiskDecision(False, "invalid_cash")
    stake = min(proposal.notional_usd, settings.bankroll_usd,
                max(cash - state.reserved_cash, 0.0) / (1 + settings.taker_fee_bps / 10_000))
    if stake < settings.min_notional_usd:
        return RiskDecision(False, "below_min_notional", stake)
    if state.open_positions + state.reserved_positions >= settings.max_open_positions:
        return RiskDecision(False, "max_open_positions", stake)
    if state.last_trade_at and (now - state.last_trade_at).total_seconds() < settings.cooldown_seconds:
        return RiskDecision(False, "cooldown", stake)
    if state.day_realized_pnl <= -(settings.bankroll_usd * settings.daily_loss_stop_pct):
        return RiskDecision(False, "daily_loss_stop", stake)
    if expected_edge_bps is None or not math.isfinite(expected_edge_bps):
        return RiskDecision(False, "edge_unknown", stake)
    required = required_edge_bps(settings, spread_bps)
    dominated = expected_edge_bps <= required
    if dominated and settings.enforce_fee_dominance:
        return RiskDecision(False, "fee_dominance", stake, required_edge_bps=required, cost_warning=True)
    return RiskDecision(True, "ok", stake, required_edge_bps=required, cost_warning=dominated)
