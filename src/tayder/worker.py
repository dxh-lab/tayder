"""Durable, single-owner approval → final checks → submit → reconcile loop."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import fcntl
import logging
import math
from pathlib import Path
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from tayder.account import Account
from tayder.approve.state import ApprovalStore
from tayder.config import Settings, load_settings
from tayder.data.market import CoinbasePublicMarket
from tayder.execute.live import LiveCoinbaseExecutor, OrderAmbiguous, OrderRejected
from tayder.execute.paper import paper_fill
from tayder.journal.db import Journal
from tayder.models import Fill, Proposal, ProposalStatus as S, Side, utcnow
from tayder.notify.discord_bot import TayderBot
from tayder.risk.gates import RiskState, check_proposal
from tayder.strategy.baseline import mean_reversion_signal, mean_reversion_snapshot

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings | None = None, *, market=None, executor=None, clock=None, jev_client=None) -> None:
        self.now = clock or utcnow
        self._shadow_lock = threading.Lock()
        self.jev_client = jev_client
        self.settings = settings or load_settings()
        self.settings.validate()
        self._lock = threading.RLock()
        self._kill_requested = threading.Event()
        self._owner = None
        self._closed = False
        path = self.settings.journal_db_path
        if path != ":memory:":
            # All runtimes sharing a journal must share this lifetime lock too.
            path = str(Path(path).resolve())
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._owner = open(path + ".lock", "a")
            try:
                fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._owner.close()
                raise RuntimeError("Another Tayder worker owns this journal") from None
        try:
            self.journal = Journal(path)
            binding = {"mode": self.settings.mode, "bankroll_usd": self.settings.bankroll_usd,
                       "key_name": self.settings.coinbase_api_key_name if self.settings.is_live else ""}
            saved_binding = self.journal.get_state("binding")
            saved_account = self.journal.get_state("account")
            if saved_binding is not None and saved_binding != binding:
                raise ValueError("Journal mode, bankroll or Coinbase key differs; use its original configuration")
            if saved_binding is None:
                # Old live fills were estimates. Do not silently declare them settled.
                if (any(f["mode"] == "live" for f in self.journal.fills())
                        or (self.settings.is_live and self.journal.proposals())):
                    raise ValueError("Legacy live journal needs reconciliation against Coinbase before migration")
                self.account = Account(self.settings.bankroll_usd, killed=self.settings.killed)
                for row in self.journal.fills():
                    from datetime import datetime
                    self.account.apply(Fill(**{**row, "side": Side(row["side"]),
                        "filled_at": datetime.fromisoformat(row["filled_at"])}))
                with self.journal.transaction():
                    for p in self.journal.proposals():
                        if p.status in (S.PENDING, S.APPROVED, S.SUBMITTING):
                            p.status = S.FAILED
                            p.meta["failure_reason"] = "legacy_proposal_requires_new_approval"
                            self.journal.save_proposal(p)
                    self.journal.set_state("account", self.account.to_dict())
                    self.journal.set_state("binding", binding)
            else:
                if saved_account is None:
                    raise ValueError("Journal is missing its durable account state")
                self.account = Account.from_dict(saved_account)
            self.store = ApprovalStore(self.settings.proposal_expiry_seconds, self.journal, lock=self._lock, clock=self.now)
            if self.account.killed or self.settings.killed:
                self._kill_requested.set()
            self.store.expire_due()
        except BaseException:
            if hasattr(self, "journal"):
                self.journal.close()
            if self._owner:
                self._owner.close()
            raise
        self.market = market or CoinbasePublicMarket(max_age_seconds=self.settings.max_book_age_seconds)
        self.executor = executor or (LiveCoinbaseExecutor(self.settings) if self.settings.is_live else None)
        self.bot: TayderBot | None = None
        # Last per-pair scan decision for /status (z, side, reason). Not durable.
        self._last_scan: dict[str, dict] = {}

    @property
    def killed(self) -> bool:
        return self._kill_requested.is_set() or self.account.killed

    def engage_kill(self) -> None:
        # Set before waiting on network/worker lock; the final POST guard sees it.
        self._kill_requested.set()
        with self._lock:
            self.account.killed = True
            with self.journal.transaction():
                self.journal.set_state("account", self.account.to_dict())
                for p in self.store.active():
                    if p.status in (S.PENDING, S.APPROVED):
                        self.store.transition(p.proposal_id, (p.status,), S.FAILED, failure_reason="kill_switch")
                self.journal.log_event("kill", {"engaged": True})

    def clear_kill(self) -> None:
        with self._lock:
            self.account.killed = False
            self.journal.set_state("account", self.account.to_dict())
            self._kill_requested.clear()
            self.journal.log_event("kill", {"engaged": False})

    def _risk_state(self, exclude: str | None = None) -> RiskState:
        active = [p for p in self.store.active() if p.proposal_id != exclude]
        buys = [p for p in active if p.side == Side.BUY]
        day = self.now().strftime("%Y-%m-%d")
        return RiskState(open_positions=len(self.account.positions),
            cash_usd=self.account.cash_usd, holdings={k: p.size for k, p in self.account.positions.items()},
            last_trade_at=self.account.last_trade_at, day_key=day,
            day_realized_pnl=self.account.realized_by_day.get(day, 0.0),
            reserved_cash=sum(p.notional_usd * (1 + self.settings.taker_fee_bps / 10_000) for p in buys),
            reserved_positions=len(buys))

    def _book(self, pair):
        book = self.market.top_of_book(pair)
        if book.product_id != pair or not all(math.isfinite(v) and v > 0 for v in
                (book.bid, book.ask, book.bid_size, book.ask_size)) or book.ask < book.bid:
            raise ValueError("invalid_book")
        age = (self.now() - book.ts).total_seconds()
        if age < -5 or age > self.settings.max_book_age_seconds:
            raise ValueError("stale_book")
        if book.spread_bps > self.settings.max_spread_bps:
            raise ValueError("spread_too_wide")
        return book

    def _check(self, p: Proposal, book, *, balances=None):
        if self.killed:
            raise ValueError("kill_switch")
        others = [q for q in self.store.active() if q.proposal_id != p.proposal_id]
        if any(q.status == S.SUBMITTING for q in others):
            raise ValueError("unresolved_order")
        if any(q.product_id == p.product_id for q in others):
            raise ValueError("inventory_reserved")
        state = self._risk_state(p.proposal_id)
        if balances is not None:
            state.cash_usd = min(state.cash_usd, balances.get("USD", 0.0))
            state.holdings = {pair: min(size, balances.get(pair.split("-")[0], 0.0))
                              for pair, size in state.holdings.items()}
        price = book.ask if p.side == Side.BUY else book.bid
        if not math.isfinite(p.signal_price) or p.signal_price <= 0:
            raise ValueError("invalid_signal_price")
        if abs(price / p.signal_price - 1) * 10_000 > self.settings.max_price_drift_bps:
            raise ValueError("price_drift")
        # Recompute distance to the original mean at today's executable price.
        edge = p.meta.get("estimated_edge_bps", p.meta.get("edge_bps"))
        if "sma" in p.meta:
            edge = (float(p.meta["sma"]) - price) / price * 10_000
        current = replace(p, signal_price=price)
        decision = check_proposal(current, replace(self.settings, killed=self.killed), state,
            now=self.now(), expected_edge_bps=float(edge) if edge is not None else None, spread_bps=book.spread_bps)
        if not decision.ok:
            raise ValueError(decision.reason)
        if decision.required_edge_bps is not None:
            p.meta["required_edge_bps"] = decision.required_edge_bps
            p.meta["executable_edge_bps"] = float(edge) if edge is not None else None
            p.meta["cost_warning"] = decision.cost_warning
        base_size = None
        stake = decision.stake_usd
        if p.side == Side.SELL:
            # The human approved base size is frozen; never sell more on a price drop.
            approved_size = float(p.meta.get("base_size", p.notional_usd / p.signal_price))
            base_size = min(approved_size, state.holdings.get(p.product_id, 0.0))
            if not math.isfinite(base_size) or base_size <= 0:
                raise ValueError("no_inventory")
            stake = base_size * price
            if stake < self.settings.min_notional_usd:
                raise ValueError("below_min_notional")
        return stake, base_size

    def _fail(self, p, reason):
        self.store.transition(p.proposal_id, (S.PENDING, S.APPROVED, S.SUBMITTING), S.FAILED,
                              failure_reason=str(reason))
        self.journal.log_event("execution_reject", {"proposal_id": p.proposal_id, "reason": str(reason)})

    def _book_fill(self, p: Proposal, fill: Fill) -> None:
        if (fill.proposal_id, fill.product_id, fill.side, fill.mode) != (
                p.proposal_id, p.product_id, p.side, self.settings.mode):
            raise ValueError("fill_identity_mismatch")
        account = deepcopy(self.account)
        account.apply(fill)
        complete = replace(p, status=S.EXECUTED, meta={**p.meta, "order_id": fill.order_id})
        if self.journal.finish(fill, complete, account.to_dict()):
            self.account = account
            # Persisted together above; now update the shared in-memory object.
            p.status, p.meta = complete.status, complete.meta
            log.info("Confirmed %s %s size=%s fee=%s", fill.side, fill.product_id, fill.size, fill.fee_usd)

    def _reconcile_one(self, p: Proposal) -> None:
        assert self.executor is not None
        try:
            order_id = p.meta.get("order_id")
            if not order_id:
                order_id = self.executor.find_order(p.proposal_id)
                if not order_id:
                    return  # Eventual visibility is not proof of rejection. Never re-POST.
                self.store.transition(p.proposal_id, (S.SUBMITTING,), S.SUBMITTING, order_id=order_id)
            fill = self.executor.reconcile(p, order_id)
            if fill is not None:
                self._book_fill(p, fill)
        except OrderRejected as exc:
            self._fail(p, exc)
        except Exception as exc:
            # A malformed fill or failed local commit cannot erase an accepted order.
            self.journal.log_event("reconciliation_pending", {"proposal_id": p.proposal_id,
                                                             "reason": type(exc).__name__})
            log.warning("Reconciliation pending for %s: %s", p.proposal_id, type(exc).__name__)

    def reconcile_pending(self) -> None:
        with self._lock:
            for p in self.store.active():
                if p.status == S.SUBMITTING:
                    if self.settings.is_live:
                        self._reconcile_one(p)
                    else:
                        # A paper crash before the atomic fill commit has no external effect.
                        self._fail(p, "interrupted_paper_execution")

    async def on_approved(self, proposal: Proposal) -> None:
        await asyncio.to_thread(self._execute_approved, proposal)

    def _execute_approved(self, proposal: Proposal) -> None:
        with self._lock:
            self.store.expire_due()
            p = self.store.get(proposal.proposal_id)
            if p is None or p.status != S.APPROVED:
                return
            submitting = False
            try:
                balances = self.executor.balances() if self.settings.is_live else None
                book = self._book(p.product_id)
                stake, base_size = self._check(p, book, balances=balances)
                if self.settings.is_live:
                    sizing = self.executor.prepare(p, stake, base_size)
                    self.executor.verify_permissions()
                    stake = float(sizing.get("quote_size", stake))
                    # Prepare must only round down. Keep actual intent durable before POST.
                    if base_size is not None:
                        base_size = float(sizing["base_size"])
                    self.store.transition(p.proposal_id, (S.APPROVED,), S.SUBMITTING,
                        stake_usd=stake, **sizing)
                    submitting = True

                    def final_guard():
                        available = self.executor.balances()
                        latest = self._book(p.product_id)
                        allowed_stake, allowed_base = self._check(p, latest, balances=available)
                        if stake > allowed_stake + 1e-9 and p.side == Side.BUY:
                            raise ValueError("cash_changed")
                        if base_size is not None and base_size > allowed_base + 1e-12:
                            raise ValueError("inventory_changed")

                    order_id = self.executor.submit(p, stake, before_submit=final_guard)
                    self.store.transition(p.proposal_id, (S.SUBMITTING,), S.SUBMITTING, order_id=order_id)
                    self._reconcile_one(p)
                else:
                    self.store.transition(p.proposal_id, (S.APPROVED,), S.SUBMITTING, stake_usd=stake)
                    submitting = True
                    self._check(p, book)
                    fill = paper_fill(p, book, stake_usd=stake,
                        base_size=base_size, taker_fee_bps=self.settings.taker_fee_bps,
                        slippage_bps=self.settings.slippage_bps, filled_at=self.now())
                    self._book_fill(p, fill)
            except (OrderRejected, ValueError) as exc:
                # These execution API exceptions guarantee no uncertain POST.
                # Booking failures following POST are handled inside reconciliation.
                self._fail(p, exc)
            except Exception as exc:
                if submitting and self.settings.is_live:
                    if isinstance(exc, OrderAmbiguous) and exc.order_id:
                        self.store.transition(p.proposal_id, (S.SUBMITTING,), S.SUBMITTING, order_id=exc.order_id)
                    self.journal.log_event("submission_uncertain", {"proposal_id": p.proposal_id,
                                                                   "reason": type(exc).__name__})
                else:
                    self._fail(p, type(exc).__name__)

    def _expected_completed_candle_start(self, now: datetime) -> datetime:
        gran = self.settings.candle_granularity_seconds
        aligned = int(now.timestamp()) // gran * gran
        return datetime.fromtimestamp(aligned - gran, tz=timezone.utc)

    def _already_proposed_candle(self, pair: str, candle_at: str | None) -> bool:
        """True if this product already got a Discord proposal for this bar."""
        if not candle_at:
            return False
        return any(
            p.product_id == pair and p.meta.get("signal_candle_at") == candle_at
            for p in self.store.all_proposals()
        )

    def _note_scan(self, pair: str, *, z: float | None, side: str | None, reason: str,
                   candle_at: str | None = None) -> None:
        self._last_scan[pair] = {
            "z": z, "side": side, "reason": reason, "candle_at": candle_at,
            "at": self.now().isoformat(),
        }

    def scan_once(self) -> list[Proposal]:
        with self._lock:
            self.store.expire_due()
            if self.killed or any(p.status == S.SUBMITTING for p in self.store.active()):
                return []
            out = []
            gran = self.settings.candle_granularity_seconds
            for pair in self.settings.strategy_pairs:
                try:
                    if any(p.product_id == pair for p in self.store.active()):
                        self._note_scan(pair, z=None, side=None, reason="inventory_reserved")
                        continue
                    candles = self.market.candles(pair, granularity=900, limit=50)
                    now = self.now()
                    # Require the latest completed 15m bar (not merely "start < 30m ago",
                    # which goes false right when the next bar should appear).
                    expected = self._expected_completed_candle_start(now)
                    if not candles or candles[-1].ts < expected:
                        raise ValueError("stale_candles")
                    signal_kwargs = dict(
                        lookback=self.settings.strategy_lookback,
                        z_entry=self.settings.strategy_z_entry,
                        # Half the configured bankroll per proposal (was $5 on a $10 book).
                        notional_usd=self.settings.bankroll_usd * 0.5,
                        granularity_seconds=gran,
                    )
                    snap = mean_reversion_snapshot(
                        candles, lookback=self.settings.strategy_lookback,
                        z_entry=self.settings.strategy_z_entry, now=now,
                        granularity_seconds=gran,
                    )
                    signal = mean_reversion_signal(pair, candles, now=now, **signal_kwargs)
                    if signal is None:
                        self._note_scan(
                            pair,
                            z=None if snap is None else snap.z,
                            side=None,
                            reason="no_signal",
                            candle_at=None if snap is None else snap.candle_at.isoformat(),
                        )
                        continue
                    candle_at = signal.meta.get("signal_candle_at")
                    # One Discord action per fresh entry: ignore continuation bars
                    # so a sustained z-score does not re-ping every bar.
                    prior = mean_reversion_signal(
                        pair, candles,
                        now=now - timedelta(seconds=gran),
                        **signal_kwargs,
                    )
                    if prior is not None and prior.side == signal.side:
                        self._note_scan(
                            pair, z=signal.meta.get("z"), side=signal.side.value,
                            reason="continuation", candle_at=candle_at,
                        )
                        continue
                    # Expiry is 5m; candles are 15m. Without candle dedupe the same
                    # fresh cross re-fires after every skip/expiry on that bar.
                    if self._already_proposed_candle(pair, candle_at):
                        self._note_scan(
                            pair, z=signal.meta.get("z"), side=signal.side.value,
                            reason="already_proposed_candle", candle_at=candle_at,
                        )
                        continue
                    book = self._book(pair)
                    stake, size = self._check(signal, book)
                    signal.notional_usd = stake
                    if size is not None:
                        signal.meta["base_size"] = size
                    marks = {pair: book.mid}
                    for held in self.account.positions:
                        if held not in marks:
                            marks[held] = self._book(held).mid
                    signal.meta["account_equity_usd"] = self.account.equity(marks)
                    signal.meta["cash_usd"] = self.account.cash_usd
                    signal.expires_at = now + timedelta(seconds=self.settings.proposal_expiry_seconds)
                    self.store.register(signal)
                    if self.settings.jev_mode == "shadow" and signal.side == Side.BUY:
                        try:
                            from tayder.decision.snapshot import make_request
                            request = make_request(signal, candles, book, self.settings.jev_model)
                            self.journal.enqueue_decision(signal.proposal_id, request)
                        except Exception as exc:
                            # Optional telemetry must never suppress an existing proposal.
                            log.warning("Shadow snapshot unavailable: %s", type(exc).__name__)
                    self._note_scan(
                        pair, z=signal.meta.get("z"), side=signal.side.value,
                        reason="proposed", candle_at=candle_at,
                    )
                    out.append(signal)
                except Exception as exc:
                    self._note_scan(pair, z=None, side=None, reason=f"reject:{exc}")
                    self.journal.log_event("scan_reject", {"product": pair, "reason": str(exc)})
            return out

    def status(self) -> str:
        with self._lock:
            parts = [
                f"mode={self.settings.mode} bankroll=${self.settings.bankroll_usd:.2f}",
                f"cash=${self.account.cash_usd:.2f} killed={self.killed}",
                f"positions={len(self.account.positions)}",
                f"unresolved={sum(p.status == S.SUBMITTING for p in self.store.active())}",
            ]
            if self._last_scan:
                scans = []
                for pair, info in self._last_scan.items():
                    z = info.get("z")
                    z_s = "n/a" if z is None else f"{z:.2f}"
                    scans.append(f"{pair} z={z_s} {info.get('reason')}")
                parts.append("scan[" + "; ".join(scans) + "]")
            else:
                parts.append("scan=none_yet")
            if self.settings.jev_mode == "shadow":
                latest = self.journal.latest_decision()
                if latest:
                    request, result = latest
                    label = result.get("answer", {}).get("choice", "unavailable")
                    parts.append(f"Jev experimental context: {request['state']['product_id']} {label} (not profit odds)")
                else:
                    parts.append("Jev shadow: awaiting assessment")
            return " ".join(parts)

    def evaluate_shadow_once(self) -> None:
        """No worker lock, trading state mutation, or authority to approve orders."""
        if self.settings.jev_mode != "shadow":
            return
        with self._shadow_lock:
            if self._closed:
                return
            job = self.journal.next_decision()
            if job is None:
                return
            from tayder.decision.jev import JevClient
            client = self.jev_client or JevClient(self.settings.typesafe_api_key,
                                                self.settings.jev_timeout_seconds)
            result = client.evaluate(job["request"])
            result["available_at"] = self.now().isoformat()
            self.journal.finish_decision(job["decision_id"], result)

    async def shadow_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.evaluate_shadow_once)
            except Exception as exc:
                log.warning("Shadow evaluation unavailable: %s", type(exc).__name__)
            await asyncio.sleep(1)

    async def loop(self) -> None:
        assert self.bot is not None
        while True:
            try:
                # Reconcile even while killed. This records existing effects, not new trades.
                await asyncio.to_thread(self.reconcile_pending)
                for p in list(self.store.active()):
                    if p.status == S.APPROVED:
                        await self.on_approved(p)
                proposals = await asyncio.to_thread(self.scan_once)
                # Also republish pending proposals that survived a crash before publication.
                for p in self.store.active():
                    if p.status == S.PENDING and (p in proposals or not p.meta.get("discord_message_id")):
                        await self.bot.publish_proposal(p, p.meta["account_equity_usd"])
            except Exception:
                log.exception("Worker iteration failed; durable state retained")
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_async(self) -> None:
        if not self.settings.discord_token or not self.settings.discord_channel_id:
            self.close()
            raise ValueError("DISCORD_TOKEN and DISCORD_CHANNEL_ID required to run worker")
        self.bot = TayderBot(self.settings, self.store, on_approved=self.on_approved,
            on_kill=self.engage_kill, on_resume=self.clear_kill, status_provider=self.status)
        try:
            async with self.bot:
                task = asyncio.create_task(self.bot.start(self.settings.discord_token))
                # Wait for login; unsent proposals retry when the channel resolves.
                ready = asyncio.create_task(self.bot.wait_until_ready())
                done, _ = await asyncio.wait((task, ready), return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    ready.cancel()
                    await task
                    return
                loop_task = asyncio.create_task(self.loop())
                shadow_task = asyncio.create_task(self.shadow_loop())
                try:
                    await task
                finally:
                    loop_task.cancel()
                    shadow_task.cancel()
                    await asyncio.gather(loop_task, shadow_task, return_exceptions=True)
        finally:
            self.close()

    def close(self) -> None:
        # Shadow requests never acquire the worker lock; wait before closing SQLite.
        with self._shadow_lock, self._lock:
            if self._closed:
                return
            self._closed = True
            self.market.close()
            if self.executor:
                self.executor.close()
            self.journal.close()
            if self._owner:
                self._owner.close()

    def run(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        asyncio.run(self.run_async())


def run_paper_dry_scan() -> None:
    """Isolated paper scan; never changes the production journal or submits orders."""
    settings = replace(load_settings(), mode="paper", journal_db_path=":memory:", jev_mode="off")
    worker = Worker(settings)
    try:
        for p in worker.scan_once():
            print(p)
    finally:
        worker.close()
