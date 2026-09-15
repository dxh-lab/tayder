"""Main paper/live loop: signal → risk → Discord approve → execute → journal."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, replace
from datetime import timedelta

from tayder.approve.state import ApprovalStore
from tayder.config import Settings, load_settings
from tayder.data.market import CoinbasePublicMarket
from tayder.execute.live import LiveCoinbaseExecutor, MissingCredentialsExecutor
from tayder.execute.paper import paper_fill
from tayder.journal.db import Journal
from tayder.models import Proposal, Side, utcnow
from tayder.notify.discord_bot import TayderBot
from tayder.risk.gates import RiskState, check_proposal
from tayder.strategy.baseline import mean_reversion_signal

log = logging.getLogger(__name__)


@dataclass
class KillSwitch:
    engaged: bool = False


class Worker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.kill = KillSwitch(False)
        self.store = ApprovalStore(
            default_ttl_seconds=self.settings.proposal_expiry_seconds
        )
        self.journal = Journal(self.settings.journal_db_path)
        self.market = CoinbasePublicMarket()
        self.risk_state = RiskState(cash_usd=self.settings.bankroll_usd)
        self._pending_stake: dict[str, float] = {}
        self._lock = threading.Lock()
        self.bot: TayderBot | None = None

    def _settings_with_kill(self) -> Settings:
        return replace(self.settings, killed=self.kill.engaged)

    def engage_kill(self) -> None:
        self.kill.engaged = True
        self.journal.log_event("kill", {"engaged": True})
        log.warning("Kill-switch engaged")

    def clear_kill(self) -> None:
        self.kill.engaged = False
        self.journal.log_event("kill", {"engaged": False})
        log.info("Kill-switch cleared")

    def _executor(self):
        if not self.settings.is_live:
            return None
        if self.settings.coinbase_api_key_name and self.settings.private_key_pem():
            return LiveCoinbaseExecutor(self.settings)
        return MissingCredentialsExecutor()

    async def on_approved(self, proposal: Proposal) -> None:
        await asyncio.to_thread(self._execute_approved, proposal)

    def _execute_approved(self, proposal: Proposal) -> None:
        with self._lock:
            stake = self._pending_stake.pop(proposal.proposal_id, proposal.notional_usd)
            try:
                book = self.market.top_of_book(proposal.product_id)
                if self.settings.is_live:
                    ex = self._executor()
                    assert ex is not None
                    fill = ex.execute(proposal, stake)
                else:
                    fill = paper_fill(
                        proposal,
                        book,
                        stake_usd=stake,
                        taker_fee_bps=self.settings.taker_fee_bps,
                    )
                self.store.mark_executed(proposal.proposal_id)
                self.journal.save_fill(fill)
                proposal.status = proposal.status  # already updated in store
                self.journal.save_proposal(self.store.get(proposal.proposal_id) or proposal)
                if proposal.side == Side.BUY:
                    self.risk_state.open_positions += 1
                    if self.risk_state.cash_usd is not None:
                        self.risk_state.cash_usd -= stake + fill.fee_usd
                else:
                    self.risk_state.open_positions = max(
                        0, self.risk_state.open_positions - 1
                    )
                    if self.risk_state.cash_usd is not None:
                        self.risk_state.cash_usd += stake - fill.fee_usd
                self.risk_state.last_trade_at = utcnow()
                self.journal.log_event(
                    "fill",
                    {"order_id": fill.order_id, "proposal_id": fill.proposal_id},
                )
                log.info("Filled %s %s @ %s", fill.side, fill.product_id, fill.price)
            except Exception:
                log.exception("Execute failed for %s", proposal.proposal_id)
                try:
                    self.store.mark_failed(proposal.proposal_id)
                    failed = self.store.get(proposal.proposal_id)
                    if failed:
                        self.journal.save_proposal(failed)
                except Exception:
                    pass

    def scan_once(self) -> list[Proposal]:
        """Generate proposals that pass risk; register for approval."""
        if self.kill.engaged:
            return []
        out: list[Proposal] = []
        settings = self._settings_with_kill()
        for pair in settings.strategy_pairs:
            try:
                candles = self.market.candles(
                    pair, granularity=settings.candle_granularity_seconds, limit=50
                )
            except Exception:
                log.exception("Candles failed for %s", pair)
                continue
            stake = min(settings.bankroll_usd, 5.0)
            signal = mean_reversion_signal(pair, candles, notional_usd=stake)
            if signal is None:
                continue
            # Fee-dominance only when strategy tags an explicit edge_bps
            edge = signal.meta.get("edge_bps")
            decision = check_proposal(
                signal,
                settings,
                self.risk_state,
                expected_edge_bps=float(edge) if edge is not None else None,
            )
            if not decision.ok:
                self.journal.log_event(
                    "risk_reject",
                    {"reason": decision.reason, "product": pair},
                )
                log.info("Risk reject %s: %s", pair, decision.reason)
                continue
            signal.notional_usd = decision.stake_usd
            signal.expires_at = utcnow() + timedelta(
                seconds=settings.proposal_expiry_seconds
            )
            self.store.register(signal, ttl_seconds=settings.proposal_expiry_seconds)
            self._pending_stake[signal.proposal_id] = decision.stake_usd
            self.journal.save_proposal(signal)
            out.append(signal)
        return out

    async def loop(self) -> None:
        assert self.bot is not None
        while True:
            self.store.expire_due()
            if not self.kill.engaged:
                proposals = await asyncio.to_thread(self.scan_once)
                for p in proposals:
                    await self.bot.publish_proposal(p)
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_async(self) -> None:
        if not self.settings.discord_token:
            log.error("DISCORD_TOKEN required to run worker")
            raise SystemExit(1)
        self.bot = TayderBot(
            self.settings,
            self.store,
            on_approved=self.on_approved,
            on_kill=self.engage_kill,
            on_resume=self.clear_kill,
        )
        async with self.bot:
            t = asyncio.create_task(self.bot.start(self.settings.discord_token))
            # wait until ready-ish
            for _ in range(50):
                if self.bot.is_ready():
                    break
                await asyncio.sleep(0.2)
            loop_task = asyncio.create_task(self.loop())
            try:
                await t
            finally:
                loop_task.cancel()
                self.market.close()
                self.journal.close()

    def run(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        log.info(
            "Starting tayder mode=%s bankroll=$%s pairs=%s",
            self.settings.mode,
            self.settings.bankroll_usd,
            self.settings.strategy_pairs,
        )
        asyncio.run(self.run_async())


def run_paper_dry_scan() -> None:
    """No-Discord one-shot scan for local smoke (still needs network for candles)."""
    logging.basicConfig(level=logging.INFO)
    w = Worker()
    props = w.scan_once()
    for p in props:
        print(p)
    w.market.close()
    w.journal.close()
