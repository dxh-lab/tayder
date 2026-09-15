"""Risk gates: bankroll, open count, notional, cooldown, daily loss, fees."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from tayder.config import Settings
from tayder.models import Proposal, Side


@dataclass
class RiskState:
    open_positions: int = 0
    last_trade_at: datetime | None = None
    day_realized_pnl: float = 0.0
    day_key: str = ""
    cash_usd: float | None = None  # paper cash; None → use bankroll


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: str
    stake_usd: float = 0.0


def _today_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def refresh_day(state: RiskState, now: datetime | None = None) -> None:
    key = _today_key(now)
    if state.day_key != key:
        state.day_key = key
        state.day_realized_pnl = 0.0


def check_proposal(
    proposal: Proposal,
    settings: Settings,
    state: RiskState,
    *,
    now: datetime | None = None,
    expected_edge_bps: float | None = None,
) -> RiskDecision:
    """Return whether proposal may proceed and the capped stake."""
    now = now or datetime.now(timezone.utc)
    refresh_day(state, now)

    if getattr(settings, "killed", False):
        return RiskDecision(False, "kill_switch")

    if proposal.side not in (Side.BUY, Side.SELL):
        return RiskDecision(False, "invalid_side")

    bankroll = settings.bankroll_usd
    if bankroll <= 0 or bankroll > 10:
        return RiskDecision(False, "bankroll_out_of_bounds")

    cash = state.cash_usd if state.cash_usd is not None else bankroll
    stake = min(float(proposal.notional_usd), bankroll, max(cash, 0.0))
    if stake < settings.min_notional_usd:
        return RiskDecision(False, "below_min_notional", stake)

    if state.open_positions >= settings.max_open_positions:
        if proposal.side == Side.BUY:
            return RiskDecision(False, "max_open_positions", stake)

    if state.last_trade_at is not None:
        elapsed = (now - state.last_trade_at).total_seconds()
        if elapsed < settings.cooldown_seconds:
            return RiskDecision(False, "cooldown", stake)

    loss_limit = -(bankroll * settings.daily_loss_stop_pct)
    if state.day_realized_pnl <= loss_limit:
        return RiskDecision(False, "daily_loss_stop", stake)

    # Fee dominance: when an expected edge is known, refuse if RT fees
    # consume it (plus fee_dominance_bps buffer). Skip if edge unknown.
    if expected_edge_bps is not None:
        rt_fee_bps = settings.taker_fee_bps * 2
        if expected_edge_bps <= rt_fee_bps + settings.fee_dominance_bps:
            return RiskDecision(False, "fee_dominance", stake)

    if settings.is_live:
        has_creds = bool(
            settings.coinbase_api_key_name and settings.private_key_pem()
        )
        if not has_creds:
            return RiskDecision(False, "live_credentials_missing", stake)

    return RiskDecision(True, "ok", stake)
