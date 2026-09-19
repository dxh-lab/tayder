"""Approval-to-ledger scenarios. No credentials, Discord service or real exchange."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import threading

import pytest

from tayder.account import Account, Position
from tayder.config import Settings
from tayder.execute.live import OrderAmbiguous, OrderRejected
from tayder.models import Candle, Fill, Proposal, ProposalStatus as S, Side, TopOfBook, utcnow
from tayder.worker import Worker


class Market:
    price = 100.0
    ask = None
    age = 0
    callback = None
    def candles(self, pair, **kwargs):
        end = utcnow().replace(second=0, microsecond=0)
        end -= timedelta(minutes=end.minute % 15)
        return [Candle(end - timedelta(minutes=15 * (20 - i)), 100, 100, 95, 95 if i == 19 else 100, 1)
                for i in range(20)]
    def top_of_book(self, pair):
        if self.callback:
            self.callback()
        return TopOfBook(pair, self.price, self.ask or self.price, 10, 10,
                         utcnow() - timedelta(seconds=self.age))
    def close(self):
        pass


class Exchange:
    def __init__(self):
        self.posts = []
        self.available = {"USD": 1000, "BTC": 50, "ETH": 50}
        self.result = None
        self.ambiguous = False
        self.visible = False
        self.guard_hook = None
    def balances(self):
        return self.available
    def prepare(self, p, stake, base_size=None):
        return {"quote_size": str(stake)} if p.side == Side.BUY else {"base_size": str(base_size or p.meta["base_size"])}
    def verify_permissions(self):
        pass
    def submit(self, p, stake, *, before_submit):
        if self.guard_hook:
            self.guard_hook()
        before_submit()
        self.posts.append((deepcopy(p), stake))
        if self.ambiguous:
            raise OrderAmbiguous("lost_response")
        return "actual-order"
    def find_order(self, pid):
        return "actual-order" if self.visible else None
    def reconcile(self, p, oid):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
    def close(self):
        pass


@pytest.fixture
def worker_factory(tmp_path):
    workers = []
    def create(*, path=None, live=False, market=None, exchange=None, **changes):
        changes.setdefault("enforce_fee_dominance", live)
        changes.setdefault("bankroll_usd", 10.0)
        settings = Settings(journal_db_path=str(path or tmp_path / f'account-{len(workers)}.db'),
            mode="live" if live else "paper", discord_allowlist=frozenset({1}),
            coinbase_api_key_name="test", coinbase_api_private_key="fake-used-by-mock-only",
            slippage_bps=0, **changes)
        w = Worker(settings, market=market or Market(), executor=exchange)
        workers.append(w)
        return w
    yield create
    for w in workers:
        if not getattr(w, '_closed', False):
            w.close()


def pending(w, side=Side.BUY, pair="BTC-USD", stake=5, **meta):
    return w.store.register(Proposal(pair, side, stake, "test", 100,
        meta={"estimated_edge_bps": 500, "account_equity_usd": 10, **meta}))


def execute(w, p):
    w.store.approve(p.proposal_id)
    w._execute_approved(p)


def saved_account(w, cash=5, size=.05, pair="BTC-USD"):
    w.account.cash_usd = cash
    w.account.positions = {pair: Position(size, 5)}
    w.journal.set_state('account', w.account.to_dict())


def _fresh_entry_signal(pair, *a, now=None, **kw):
    """Mock: signal on the current bar only so edge-trigger tests stay stable."""
    if now is not None and (utcnow() - now).total_seconds() > 60:
        return None
    notional = float(kw.get("notional_usd", 5))
    return Proposal(pair, Side.BUY, notional, 'test', 100,
                    meta={'estimated_edge_bps': 500, 'sma': 105})


def test_buy_sell_roundtrip_books_actual_cash_inventory_fees_and_loss(worker_factory):
    w = worker_factory()
    buy = pending(w)
    execute(w, buy)
    assert buy.status == S.EXECUTED
    assert w.account.cash_usd == pytest.approx(4.97)
    assert w.account.positions['BTC-USD'].size == .05
    assert w.account.equity({'BTC-USD': 100}) == pytest.approx(9.97)
    sell = pending(w, Side.SELL, base_size=.05)
    execute(w, sell)
    assert sell.status == S.EXECUTED
    assert w.account.positions == {}
    assert w.account.cash_usd == pytest.approx(9.94)
    assert w.account.realized_by_day[utcnow().strftime('%Y-%m-%d')] == pytest.approx(-.06)
    assert len(w.journal.fills()) == 2


def test_duplicate_callbacks_and_forged_payload_only_execute_stored_approval_once(worker_factory):
    w = worker_factory()
    p = pending(w)
    w.store.approve(p.proposal_id)
    forged = replace(p, product_id='ETH-USD', notional_usd=10)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(w._execute_approved, [forged] * 20))
    assert len(w.journal.fills()) == 1
    assert set(w.account.positions) == {'BTC-USD'}
    assert w.account.cash_usd == pytest.approx(4.97)


def test_unapproved_cannot_execute(worker_factory):
    w = worker_factory()
    p = pending(w)
    w._execute_approved(p)
    assert p.status == S.PENDING and not w.journal.fills()


def test_same_scan_and_later_scan_reserve_one_position(worker_factory, monkeypatch):
    w = worker_factory()
    monkeypatch.setattr('tayder.worker.mean_reversion_signal', _fresh_entry_signal)
    proposals = w.scan_once()
    assert len(proposals) == 1
    assert w.scan_once() == []
    assert w._risk_state().reserved_cash == pytest.approx(5.03)
    w.store.skip(proposals[0].proposal_id)
    assert len(w.scan_once()) == 1


def test_paper_scan_uses_half_bankroll_notional(worker_factory, monkeypatch):
    w = worker_factory(bankroll_usd=100, enforce_fee_dominance=False)
    monkeypatch.setattr('tayder.worker.mean_reversion_signal', _fresh_entry_signal)
    p, = w.scan_once()
    assert p.notional_usd == pytest.approx(50.0)
    assert w.account.cash_usd == pytest.approx(100.0)


def test_scan_skips_continuation_bars_without_re_notifying(worker_factory, monkeypatch):
    w = worker_factory()
    monkeypatch.setattr(
        'tayder.worker.mean_reversion_signal',
        lambda pair, *a, now=None, **kw: Proposal(
            pair, Side.BUY, 5, 'test', 100, meta={'estimated_edge_bps': 500, 'sma': 105}),
    )
    assert w.scan_once() == []


def test_paper_scan_proposes_when_fees_dominate_if_not_enforced(worker_factory, monkeypatch):
    w = worker_factory(enforce_fee_dominance=False, taker_fee_bps=60, fee_dominance_bps=20)
    def low_edge(pair, *a, now=None, **kw):
        if now is not None and (utcnow() - now).total_seconds() > 60:
            return None
        return Proposal(pair, Side.BUY, 5, 'test', 100, meta={'estimated_edge_bps': 40, 'sma': 100.4})
    monkeypatch.setattr('tayder.worker.mean_reversion_signal', low_edge)
    p, = w.scan_once()
    assert p.meta.get('cost_warning') is True
    assert p.meta.get('required_edge_bps') == pytest.approx(140)  # 2*60 + 20; factory sets slippage=0


def test_live_scan_still_hard_blocks_fee_dominance(worker_factory, monkeypatch):
    w = worker_factory(live=True, enforce_fee_dominance=True)
    def low_edge(pair, *a, now=None, **kw):
        if now is not None and (utcnow() - now).total_seconds() > 60:
            return None
        return Proposal(pair, Side.BUY, 5, 'test', 100, meta={'estimated_edge_bps': 40, 'sma': 100.4})
    monkeypatch.setattr('tayder.worker.mean_reversion_signal', low_edge)
    assert w.scan_once() == []


def test_restored_pending_reservations_skip_expiry_and_kill(worker_factory):
    w = worker_factory()
    p = pending(w)
    skipped = pending(w, pair='ETH-USD')
    w.store.skip(skipped.proposal_id)
    path = w.settings.journal_db_path
    w.close()
    restored = worker_factory(path=path)
    assert restored._risk_state().reserved_positions == 1
    assert restored.store.get(skipped.proposal_id).status == S.SKIPPED
    restored.store.expire_due(now=utcnow() + timedelta(hours=1))
    assert restored._risk_state().reserved_cash == 0
    restored.engage_kill()
    restored.close()
    again = worker_factory(path=path)
    assert again.killed
    assert again.store.get(p.proposal_id).status == S.EXPIRED
    again.clear_kill()
    assert not again.killed


def test_restore_cash_inventory_cooldown_and_daily_loss(worker_factory):
    w = worker_factory()
    p = pending(w)
    execute(w, p)
    before = w.account.to_dict()
    path = w.settings.journal_db_path
    w.close()
    restored = worker_factory(path=path)
    assert restored.account.to_dict() == before
    assert restored._risk_state().open_positions == 1
    assert restored._risk_state().day_realized_pnl == pytest.approx(-.03)
    assert restored._risk_state().last_trade_at is not None
    restored._execute_approved(p)
    assert len(restored.journal.fills()) == 1


@pytest.mark.parametrize('reason', ['kill', 'expiry', 'cash', 'inventory', 'loss', 'cooldown', 'stale', 'drift', 'spread'])
def test_final_checks_reject_changed_conditions(worker_factory, reason):
    w = worker_factory()
    p = pending(w)
    w.store.approve(p.proposal_id)
    if reason == 'kill':
        w.engage_kill()
    elif reason == 'expiry':
        p.expires_at = utcnow() - timedelta(seconds=1)
    elif reason == 'cash':
        w.account.cash_usd = .1
    elif reason == 'inventory':
        saved_account(w, pair='ETH-USD')
    elif reason == 'loss':
        w.account.realized_by_day[utcnow().strftime('%Y-%m-%d')] = -2.5
    elif reason == 'cooldown':
        w.account.last_trade_at = utcnow()
    elif reason == 'stale':
        w.market.age = 61
    elif reason == 'drift':
        w.market.price = 110
    elif reason == 'spread':
        w.market.ask = 101
    w._execute_approved(p)
    assert p.status in (S.FAILED, S.EXPIRED)
    assert not w.journal.fills()


def test_sell_requires_matching_inventory_and_cannot_oversell(worker_factory):
    w = worker_factory()
    saved_account(w)
    wrong = pending(w, Side.SELL, pair='ETH-USD')
    execute(w, wrong)
    assert wrong.status == S.FAILED
    p = pending(w, Side.SELL, stake=10, base_size=.1)
    execute(w, p)
    assert p.status == S.EXECUTED
    assert w.journal.fills()[0]['size'] == .05
    assert w.account.positions == {}


def test_pending_sells_reserve_inventory_and_exit_allowed_after_loss_stop(worker_factory):
    w = worker_factory()
    saved_account(w)
    w.account.realized_by_day[utcnow().strftime('%Y-%m-%d')] = -4
    a, b = pending(w, Side.SELL), pending(w, Side.SELL)
    execute(w, a)
    assert a.status == S.FAILED and a.meta['failure_reason'] == 'inventory_reserved'
    execute(w, b)
    assert b.status == S.EXECUTED


def test_live_pending_then_partial_actual_fill_books_once(worker_factory):
    ex = Exchange()
    w = worker_factory(live=True, exchange=ex)
    p = pending(w)
    execute(w, p)
    assert p.status == S.SUBMITTING and len(ex.posts) == 1
    assert w.account.cash_usd == 10
    assert w.scan_once() == []
    w._execute_approved(p)
    assert len(ex.posts) == 1
    ex.result = Fill(p.proposal_id, p.product_id, p.side, 99, .03, .027, 2.97, 'live', 'actual-order')
    w.reconcile_pending()
    w.reconcile_pending()
    assert p.status == S.EXECUTED
    assert w.account.cash_usd == pytest.approx(7.003)
    assert w.account.positions[p.product_id].size == .03
    assert w.journal.fills()[0]['price'] == 99
    assert len(w.journal.fills()) == 1


def test_lost_post_response_restart_and_kill_reconcile_without_resubmission(worker_factory):
    ex = Exchange()
    ex.ambiguous = True
    w = worker_factory(live=True, exchange=ex)
    p = pending(w)
    execute(w, p)
    assert p.status == S.SUBMITTING
    path = w.settings.journal_db_path
    w.engage_kill()
    w.close()
    restored = worker_factory(path=path, live=True, exchange=ex)
    restored.reconcile_pending()
    assert restored.store.get(p.proposal_id).status == S.SUBMITTING
    ex.visible = True
    ex.result = Fill(p.proposal_id, p.product_id, p.side, 100, .05, .03, 5, 'live', 'actual-order')
    restored.reconcile_pending()
    assert restored.killed
    assert len(ex.posts) == 1 and len(restored.journal.fills()) == 1
    assert restored.account.cash_usd == pytest.approx(4.97)


def test_live_sell_caps_strategy_inventory_to_exchange_available(worker_factory):
    ex = Exchange()
    ex.available['BTC'] = .02
    w = worker_factory(live=True, exchange=ex)
    saved_account(w)
    p = pending(w, Side.SELL, base_size=.05)
    execute(w, p)
    assert ex.posts[0][0].meta['base_size'] == '0.02'
    assert p.status == S.SUBMITTING


def test_live_cash_uses_smaller_exchange_balance_and_fees(worker_factory):
    ex = Exchange()
    ex.available['USD'] = 2
    w = worker_factory(live=True, exchange=ex)
    p = pending(w)
    execute(w, p)
    assert ex.posts[0][1] * 1.006 == pytest.approx(2)


@pytest.mark.parametrize('change', ['kill', 'expire', 'drift'])
def test_last_guard_runs_after_exchange_preflight_before_post(worker_factory, change):
    ex = Exchange()
    w = worker_factory(live=True, exchange=ex)
    p = pending(w)
    def hook():
        if change == 'kill':
            w.engage_kill()
        elif change == 'expire':
            p.expires_at = utcnow() - timedelta(seconds=1)
        else:
            w.market.price = 101
    ex.guard_hook = hook
    execute(w, p)
    assert not ex.posts
    assert p.status == S.FAILED


def test_kill_requested_during_network_wait_prevents_post(worker_factory):
    ex = Exchange()
    w = worker_factory(live=True, exchange=ex)
    p = pending(w)
    entered = threading.Event()
    release = threading.Event()
    def hook():
        entered.set()
        assert release.wait(3)
    ex.guard_hook = hook
    with ThreadPoolExecutor(2) as pool:
        execution = pool.submit(execute, w, p)
        assert entered.wait(3)
        killing = pool.submit(w.engage_kill)
        assert w._kill_requested.wait(3)
        release.set()
        execution.result(3)
        killing.result(3)
    assert not ex.posts and w.killed


def test_local_commit_failure_retains_intent_until_retry(worker_factory, monkeypatch):
    ex = Exchange()
    w = worker_factory(live=True, exchange=ex)
    p = pending(w)
    ex.result = Fill(p.proposal_id, p.product_id, p.side, 100, .05, .03, 5, 'live', 'actual-order')
    original = w.journal.finish
    monkeypatch.setattr(w.journal, 'finish', lambda *a: (_ for _ in ()).throw(RuntimeError('disk')))
    execute(w, p)
    assert p.status == S.SUBMITTING and w.account.cash_usd == 10
    monkeypatch.setattr(w.journal, 'finish', original)
    w.reconcile_pending()
    assert p.status == S.EXECUTED and len(ex.posts) == 1


def test_actual_fill_booking_and_status_roll_back_together(worker_factory, monkeypatch):
    ex = Exchange()
    w = worker_factory(live=True, exchange=ex)
    p = pending(w)
    ex.result = Fill(p.proposal_id, p.product_id, p.side, 100, .05, .03, 5, 'live', 'actual-order')
    original = w.journal.set_state
    monkeypatch.setattr(w.journal, 'set_state', lambda *a: (_ for _ in ()).throw(RuntimeError('disk')))
    execute(w, p)
    assert not w.journal.fills()
    assert w.journal.proposals()[0].status == S.SUBMITTING
    assert w.account.cash_usd == 10
    monkeypatch.setattr(w.journal, 'set_state', original)
    w.reconcile_pending()
    assert len(w.journal.fills()) == 1


def test_terminal_rejection_releases_reservation(worker_factory):
    ex = Exchange()
    ex.result = OrderRejected('canceled_no_fill')
    w = worker_factory(live=True, exchange=ex)
    p = pending(w)
    execute(w, p)
    assert p.status == S.FAILED and w._risk_state().reserved_cash == 0


def test_one_worker_per_journal_and_binding_cannot_change(worker_factory):
    w = worker_factory()
    settings = w.settings
    with pytest.raises(RuntimeError, match='owns'):
        Worker(settings, market=Market())
    w.close()
    for change in ({'mode': 'live'}, {'bankroll_usd': 9}):
        with pytest.raises(ValueError, match='differs'):
            Worker(replace(settings, **change), market=Market(), executor=Exchange())


def test_paper_interrupted_intent_never_replays(worker_factory):
    w = worker_factory()
    p = pending(w)
    w.store.approve(p.proposal_id)
    w.store.transition(p.proposal_id, (S.APPROVED,), S.SUBMITTING)
    w.reconcile_pending()
    assert p.status == S.FAILED and not w.journal.fills()


def test_equity_display_includes_holdings(worker_factory, monkeypatch):
    w = worker_factory()
    saved_account(w)
    def sell_signal(pair, *a, now=None, **kw):
        if now is not None and (utcnow() - now).total_seconds() > 60:
            return None
        return Proposal(pair, Side.SELL, 5, 'test', 100, meta={'estimated_edge_bps': 500})
    monkeypatch.setattr('tayder.worker.mean_reversion_signal', sell_signal)
    p, = w.scan_once()
    assert p.meta['account_equity_usd'] == 10
    assert p.meta['cash_usd'] == 5


def test_exchange_cash_rechecked_after_intent_before_post(worker_factory):
    ex = Exchange()
    w = worker_factory(live=True, exchange=ex)
    ex.guard_hook = lambda: ex.available.update(USD=1)
    p = pending(w)
    execute(w, p)
    assert not ex.posts
    assert p.status == S.FAILED


def test_legacy_live_estimates_refuse_automatic_replay(tmp_path):
    from tayder.journal.db import Journal
    from tayder.models import Fill
    path = tmp_path / 'legacy.db'
    journal = Journal(str(path))
    p = Proposal('BTC-USD', Side.BUY, 5, 'test', 100, status=S.EXECUTED)
    journal.finish(Fill(p.proposal_id, p.product_id, p.side, 100, .05, .03, 5, 'live', 'legacy-order'), p, {})
    journal.close()
    with pytest.raises(ValueError, match='Legacy live'):
        Worker(Settings(journal_db_path=str(path)), market=Market())


def test_current_mode_and_original_cash_cannot_be_reset_by_restart(worker_factory):
    w = worker_factory()
    execute(w, pending(w))
    w.engage_kill()
    original = w.account.to_dict()
    path = w.settings.journal_db_path
    w.close()
    restored = worker_factory(path=path)
    assert restored.account.to_dict() == original
    assert restored.killed
