"""Offline replay of the real paper Worker with synthetic quotes and approvals."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

from tayder.approve.state import ApprovalError
from tayder.config import Settings
from tayder.models import TopOfBook, ProposalStatus, Side
from tayder.research import _validate_candles, load_candles_csv
from tayder.worker import Worker


class ReplayMarket:
    def __init__(self, data, clock, spread_bps):
        self.data, self.clock, self.spread_bps = data, clock, spread_bps
        self.index = 0

    def candles(self, pair, *, limit=50, **kwargs):
        # Only completed bars; current OHLC/volume are never signal inputs.
        return self.data[pair][max(0, self.index - limit):self.index]

    def top_of_book(self, pair):
        price = self.data[pair][self.index].open
        half = self.spread_bps / 20000
        return TopOfBook(pair, price * (1 - half), price * (1 + half), 1, 1, self.clock())

    def close(self):
        pass


def replay(data, *, settings=None, spread_bps=4.0, approval_latency_seconds=60,
           policy="baseline", observations=None, confidence_threshold=.8):
    """Single shared account. Optional filters are offline comparisons only."""
    settings = settings or Settings(bankroll_usd=10, enforce_fee_dominance=True)
    settings = replace(settings, mode="paper", journal_db_path=":memory:", jev_mode="off",
                       strategy_pairs=tuple(data))
    settings.validate()
    if (not math.isfinite(spread_bps) or not 0 <= spread_bps < 20000
            or not math.isfinite(approval_latency_seconds) or approval_latency_seconds < 0):
        raise ValueError("invalid replay spread/latency")
    if policy not in ("baseline", "simple_trend", "jev"):
        raise ValueError("invalid replay policy")
    if not math.isfinite(confidence_threshold) or not 0 <= confidence_threshold <= 1:
        raise ValueError("invalid confidence threshold")
    if policy == "jev" and observations is None:
        raise ValueError("Jev comparison requires recorded observations; never calls the API")
    if not data:
        raise ValueError("no assets")
    for pair, bars in data.items():
        _validate_candles(pair, bars)
    reference = next(iter(data.values()))
    if any([c.ts for c in bars] != [c.ts for c in reference] for bars in data.values()):
        raise ValueError("assets must have identical coverage")
    start = settings.strategy_lookback + 1
    if len(reference) <= start:
        raise ValueError("insufficient bars after warmup")
    now = reference[start].ts
    market = ReplayMarket(data, lambda: now, spread_bps)
    worker = Worker(settings, market=market, clock=lambda: now)
    curve, scans, filtered, missing = [], [], 0, 0
    due, trend_down = {}, {}

    def decide(p):
        nonlocal filtered, missing
        skip = False
        if p.side == Side.BUY and policy == "simple_trend":
            skip = trend_down[p.proposal_id]
        elif p.side == Side.BUY and policy == "jev":
            item = (observations or {}).get((p.product_id, p.meta["signal_candle_at"]))
            if item is None or datetime.fromisoformat(item["available_at"]) > now:
                missing += 1
                skip = True
            else:
                answer = item["answer"]
                skip = answer["choice"] != "range_bound" or answer["confidence"] < confidence_threshold
        if skip:
            filtered += 1
            worker.store.skip(p.proposal_id)
        else:
            worker.store.approve(p.proposal_id)
            worker._execute_approved(p)

    first = now
    end = reference[-1].ts + timedelta(seconds=900)
    try:
        while now < end:
            market.index = int((now - reference[0].ts).total_seconds() // 900)
            worker.store.expire_due()
            for p in list(worker.store.active()):
                if p.status == ProposalStatus.PENDING and due.get(p.proposal_id, end) <= now:
                    try:
                        decide(p)
                    except ApprovalError:
                        pass
            proposals = worker.scan_once()
            for p in proposals:
                closes = [c.close for c in market.candles(p.product_id)][-settings.strategy_lookback:]
                trend_down[p.proposal_id] = closes[-1] < closes[0]
                due[p.proposal_id] = now + timedelta(seconds=approval_latency_seconds)
                if approval_latency_seconds == 0:
                    decide(p)
            marks = {pair: market.top_of_book(pair).mid for pair in data}
            curve.append({"at": now.isoformat(), "equity": worker.account.equity(marks),
                          "exposed": bool(worker.account.positions)})
            scans.append({"at": now.isoformat(), "decisions": dict(worker._last_scan)})
            now += timedelta(seconds=settings.poll_interval_seconds)
        # Final marks become known only at the end of the final interval.
        equity = worker.account.equity({pair: bars[-1].close for pair, bars in data.items()})
        peak, drawdown = settings.bankroll_usd, 0.0
        for value in [r["equity"] for r in curve] + [equity]:
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
        fills = worker.journal.fills()
        # Remove random UUIDs so identical data/settings produce identical reports.
        ordered = worker.store.all_proposals()
        ids = {p.proposal_id: i + 1 for i, p in enumerate(ordered)}
        clean_fills = [{**{k: v for k, v in f.items() if k not in ("order_id", "proposal_id")},
                        "proposal_number": ids[f["proposal_id"]]} for f in fills]
        parameters = {k: v for k, v in asdict(settings).items()
                      if not any(s in k for s in ("key", "token", "discord", "journal"))}
        return {
            "schema_version": 1, "method": "paper_worker_replay", "policy": policy,
            "settings": parameters, "spread_bps": spread_bps,
            "approval_latency_seconds": approval_latency_seconds,
            "confidence_threshold": confidence_threshold,
            "observations_sha256": hashlib.sha256(json.dumps(
                sorted((str(k), v) for k, v in (observations or {}).items()),
                sort_keys=True, allow_nan=False).encode()).hexdigest() if observations is not None else None,
            "data_sha256": hashlib.sha256(json.dumps({p: [asdict(c) for c in bars] for p, bars in data.items()},
                sort_keys=True, default=str).encode()).hexdigest(),
            "start_at": first.isoformat(), "end_at": end.isoformat(),
            "equity_usd": equity, "return_pct": (equity / settings.bankroll_usd - 1) * 100,
            "max_drawdown_pct": drawdown * 100, "fees_usd": sum(f["fee_usd"] for f in fills),
            "turnover_usd": sum(f["notional_usd"] for f in fills),
            "exposure_fraction": sum(r["exposed"] for r in curve) / len(curve),
            "filtered_buys": filtered, "missing_observations": missing,
            "fills": clean_fills, "equity_curve": curve, "scans": scans,
            "proposals": [{"number": ids[p.proposal_id], "product_id": p.product_id,
                "side": p.side.value, "created_at": p.created_at.isoformat(),
                "status": p.status.value, "meta": {k: v for k, v in p.meta.items() if k != "order_id"}} for p in ordered],
            "limitations": [
                "Unvalidated strategy; no profitability claim or parameter selection.",
                "Synthetic quotes hold each bar open constant within that bar; no intrabar price path is known.",
                "Approval delay rounds up to the next poll; all unfiltered proposals are hypothetically approved.",
                "Uses actual paper-worker gates, shared cash, pair ordering, reservations and fee-date accounting.",
                "No historical order book, partial fills, exchange precision, taxes, or human decision model.",
                "Terminal inventory remains marked, unsold. Jev filtering exists only in this offline replay.",
            ],
        }
    finally:
        worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approval-latency-seconds", type=float, default=60)
    parser.add_argument("--spread-bps", type=float, default=4)
    parser.add_argument("--compare-simple", action="store_true")
    parser.add_argument("--jev-observations", type=Path,
                        help="Exported decisions JSON; adds an offline Jev comparison")
    parser.add_argument("--confidence-threshold", type=float, default=.8)
    args = parser.parse_args(argv)
    data = load_candles_csv(args.csv)
    policies = ("baseline", "simple_trend") if args.compare_simple else ("baseline",)
    observations = None
    if args.jev_observations:
        from tayder.decision.jev import validate_response
        observations = {}
        versions = set()
        for item in json.loads(args.jev_observations.read_text()):
            result = item["result"]
            if not result or result["status"] != "ok":
                continue
            validate_response({"model": result["model"], "answers": {"regime": result["answer"]}})
            available = datetime.fromisoformat(result["available_at"])
            if available.tzinfo is None:
                raise ValueError("observation availability requires timezone")
            state = item["request"]["state"]
            versions.add((result["model"], json.dumps(item["request"]["questions"], sort_keys=True),
                          state.get("question_version")))
            if len(versions) > 1:
                raise ValueError("select one frozen model/question version for comparison")
            key = (state["product_id"], state["last_closed_candle_at"])
            if key in observations:
                raise ValueError("duplicate observation; select one frozen model/question version")
            observations[key] = result
        policies += ("jev",)
    report = {p: replay(data, policy=p, spread_bps=args.spread_bps,
                       approval_latency_seconds=args.approval_latency_seconds, observations=observations,
                       confidence_threshold=args.confidence_threshold) for p in policies}
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


if __name__ == "__main__":
    main()
