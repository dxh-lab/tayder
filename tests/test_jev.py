from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import threading

import httpx
import pytest

from tayder.config import Settings, load_settings
from tayder.decision.jev import JevClient, validate_response
from tayder.decision.snapshot import make_request, OPTIONS
from tayder.journal.db import Journal
from tayder.models import Proposal, Side, utcnow
from tayder.worker import Worker
from tests.test_worker import Market


def response():
    return {"model": "jev-test", "answers": {"regime": {
        "type": "choice", "choice": "range_bound", "confidence": .8,
        "probabilities": {"range_bound": .9, "uptrend": .03, "downtrend": .02, "unclear": .05}}},
        "usage": {"input_tokens": 100, "output_tokens": 10}}


def test_adapter_contract_and_safe_failure():
    def handle(req):
        assert req.url == "https://api.typesafe.ai/v1/systemone"
        assert req.headers["Authorization"] == "Bearer private"
        assert json.loads(req.content)["model"] == "jev-test"
        return httpx.Response(200, json=response())
    result = JevClient("private", transport=httpx.MockTransport(handle)).evaluate({"model": "jev-test"})
    assert result["status"] == "ok" and result["model"] == "jev-test"
    for code in (302, 401, 429, 500):
        client = JevClient("private", transport=httpx.MockTransport(lambda r: httpx.Response(code, text="private")))
        result = client.evaluate({})
        assert result["status"] == "unavailable" and "private" not in json.dumps(result)


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(model=""),
    lambda x: x["answers"]["regime"].update(choice="buy"),
    lambda x: x["answers"]["regime"].update(confidence=float("nan")),
    lambda x: x["answers"]["regime"]["probabilities"].update(range_bound=.1),
    lambda x: x["answers"]["regime"]["probabilities"].update(range_bound=True),
])
def test_malformed_response_rejected(mutation):
    data = response()
    mutation(data)
    with pytest.raises(ValueError):
        validate_response(data)


def test_timeout_is_unavailable():
    def timeout(req):
        raise httpx.ReadTimeout("private")
    assert JevClient("secret", transport=httpx.MockTransport(timeout)).evaluate({})["status"] == "unavailable"


def test_snapshot_excludes_future_and_private_state():
    market = Market()
    bars = market.candles("BTC-USD")
    p = Proposal("BTC-USD", Side.BUY, 5, "private", 95,
                 meta={"cash_usd": 10, "discord_user": "private"})
    request = make_request(p, bars, market.top_of_book("BTC-USD"), "jev-test")
    future = replace(bars[-1], ts=utcnow() + timedelta(days=1), close=99999)
    assert make_request(p, bars + [future], market.top_of_book("BTC-USD"), "jev-test")["state"]["closes"] == request["state"]["closes"]
    assert "private" not in json.dumps(request) and "cash_usd" not in json.dumps(request)
    bars[-1] = replace(bars[-1], close=1)
    assert request["state"]["closes"][-1] == 95


def test_durable_cache(tmp_path):
    path = str(tmp_path / "journal.db")
    j = Journal(path)
    key = j.enqueue_decision("a", {"model": "one"})
    assert j.enqueue_decision("a", {"model": "one"}) == key
    j.finish_decision(key, {"status": "unavailable"})
    j.close()
    j = Journal(path)
    assert j.next_decision() is None
    assert len(j.decisions()) == 1
    assert j.enqueue_decision("a", {"model": "two"}) != key
    j.close()


def test_shadow_network_does_not_hold_worker_lock_or_trade(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Client:
        def evaluate(self, request):
            entered.set()
            assert release.wait(3)
            return validate_response(response())
    settings = Settings(journal_db_path=str(tmp_path / "j.db"), jev_mode="shadow", typesafe_api_key="test")
    w = Worker(settings, market=Market(), jev_client=Client())
    w.journal.enqueue_decision("test", {"model": "jev-test"})
    try:
        with ThreadPoolExecutor(2) as pool:
            evaluation = pool.submit(w.evaluate_shadow_once)
            assert entered.wait(3)
            killing = pool.submit(w.engage_kill)
            killing.result(1)
            assert w.killed and not w.journal.fills()
            release.set()
            evaluation.result(3)
        assert w.journal.decisions()[0]["result"]["status"] == "ok"
    finally:
        release.set()
        w.close()


def test_modes_and_env(monkeypatch):
    monkeypatch.setenv("JEV_MODE", "shadow")
    monkeypatch.setenv("TYPESAFE_API_KEY", "hidden")
    s = load_settings()
    assert s.jev_mode == "shadow" and "hidden" not in repr(s)
    for changes in ({"jev_mode": "filter"}, {"jev_mode": "shadow"}, {"jev_timeout_seconds": float("inf")}):
        with pytest.raises(ValueError):
            replace(Settings(), **changes).validate()


def test_actual_scan_enqueues_only_buys_and_shadow_off_is_inert(tmp_path):
    from datetime import datetime, timezone
    from tests.test_replay import data
    from tayder.replay import ReplayMarket
    d = data()
    now = d['BTC-USD'][23].ts
    market = ReplayMarket(d, lambda: now, 4)
    market.index = 23
    for mode in ('off', 'shadow'):
        s = Settings(journal_db_path=str(tmp_path / f'{mode}.db'), jev_mode=mode,
                     typesafe_api_key='test', enforce_fee_dominance=False,
                     strategy_pairs=('BTC-USD',))
        w = Worker(s, market=market, clock=lambda: now)
        try:
            p, = w.scan_once()
            assert len(w.journal.decisions()) == (1 if mode == 'shadow' else 0)
            w.store.skip(p.proposal_id)
            assert not w.scan_once()
            assert not w.journal.fills()
        finally:
            w.close()


def test_approval_can_execute_while_model_is_waiting(tmp_path):
    from tests.test_worker import pending
    entered, release = threading.Event(), threading.Event()
    class Client:
        def evaluate(self, request):
            entered.set()
            assert release.wait(3)
            return {"status": "unavailable"}
    w = Worker(Settings(journal_db_path=str(tmp_path / 'j.db'), jev_mode='shadow',
                        typesafe_api_key='test'), market=Market(), jev_client=Client())
    p = pending(w)
    w.journal.enqueue_decision(p.proposal_id, {})
    try:
        with ThreadPoolExecutor(2) as pool:
            evaluation = pool.submit(w.evaluate_shadow_once)
            assert entered.wait(3)
            def approve():
                w.store.approve(p.proposal_id)
                w._execute_approved(p)
            pool.submit(approve).result(1)
            assert len(w.journal.fills()) == 1
            release.set()
            evaluation.result(3)
    finally:
        release.set()
        w.close()
