"""Offline, deterministic research; no exchange, credentials, or new dependencies.

Public API: load_candles_csv(path, product_id=None), backtest(product_id, candles,
parameters=..., config=..., start=0, end=None), run_research(candles_by_asset,
parameters=..., config=..., train_bars=..., test_bars=..., walk_forward=True),
format_summary(report), and main(argv=None).

CSV headers: product_id, timestamp, open, high, low, close, volume. ``ts`` or
``start`` can replace timestamp; timestamps are interval starts, either Unix
seconds or ISO 8601 with a timezone. For a single-asset file, product_id may be
supplied separately. Rows must be chronological within each asset, on a complete
15-minute grid. Multiple assets must have identical coverage for fair comparison.

Example: python -m tayder.research --csv candles.csv --train-bars 672
    --test-bars 96 --lookbacks 10,20,30 --z-entries 1,1.5,2 --output report.json

Each evaluation starts with fresh cash. Prior candles warm up indicators only.
Parameter selection uses training return exclusively; test sensitivity is
exploratory and must not be used to claim out-of-sample validation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Callable, Mapping, Sequence

from tayder.models import Candle, Proposal, Side
from tayder.strategy.baseline import mean_reversion_signal

INTERVAL_SECONDS = 900
INTERVAL = timedelta(seconds=INTERVAL_SECONDS)
ASSETS = {"BTC-USD", "ETH-USD"}


def _positive(value: float, name: str, *, zero: bool = False) -> None:
    if not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")


def _integer(value: int, name: str, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class StrategyParameters:
    lookback: int = 20
    z_entry: float = 1.5
    notional_usd: float = 5.0

    def __post_init__(self) -> None:
        _integer(self.lookback, "lookback", 2)
        _positive(self.z_entry, "z_entry")
        _positive(self.notional_usd, "notional_usd")


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash_usd: float = 10.0
    fee_bps: float = 60.0
    spread_bps: float = 4.0  # full bid/ask spread; each fill pays half
    slippage_bps: float = 2.0
    approval_latency_seconds: float = 60.0
    reject_every: int = 0  # every Nth submitted order; 0 disables
    partial_every: int = 0
    partial_fraction: float = 0.5
    min_edge_buffer_bps: float | None = 20.0  # None disables the BUY distance filter

    def __post_init__(self) -> None:
        _positive(self.initial_cash_usd, "initial_cash_usd")
        for name in ("fee_bps", "spread_bps", "slippage_bps", "approval_latency_seconds"):
            _positive(getattr(self, name), name, zero=True)
        if self.fee_bps >= 10_000 or self.spread_bps / 2 + self.slippage_bps >= 10_000:
            raise ValueError("fees and one-way price impact must be below 10000 bps")
        _integer(self.reject_every, "reject_every", 0)
        _integer(self.partial_every, "partial_every", 0)
        _positive(self.partial_fraction, "partial_fraction")
        if self.partial_fraction > 1:
            raise ValueError("partial_fraction must be <= 1")
        if self.min_edge_buffer_bps is not None:
            _positive(self.min_edge_buffer_bps, "min_edge_buffer_bps", zero=True)


def _validate_candles(product_id: str, candles: Sequence[Candle]) -> None:
    if product_id not in ASSETS:
        raise ValueError(f"unsupported asset {product_id!r}; use BTC-USD or ETH-USD")
    if not candles:
        raise ValueError(f"{product_id}: no candles")
    previous = None
    for candle in candles:
        ts = candle.ts
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("candle timestamps must be timezone-aware")
        if ts.timestamp() % INTERVAL_SECONDS != 0:
            raise ValueError("candle timestamps must align to a 15-minute grid")
        if previous is not None and ts - previous != INTERVAL:
            raise ValueError(f"{product_id}: candles must be chronological, unique, without gaps")
        previous = ts
        for name in ("open", "high", "low", "close"):
            _positive(getattr(candle, name), name)
        _positive(candle.volume, "volume", zero=True)
        if candle.low > min(candle.open, candle.close) or candle.high < max(candle.open, candle.close):
            raise ValueError("OHLC prices must lie within low/high")


def _timestamp(raw: str) -> datetime:
    try:
        numeric = float(raw)
    except ValueError:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("ISO timestamps require a timezone")
        return result.astimezone(timezone.utc)
    return datetime.fromtimestamp(numeric, tz=timezone.utc)


def load_candles_csv(
    path: str | Path, product_id: str | None = None,
) -> dict[str, list[Candle]]:
    """Load and validate a combined or single-asset CSV without reordering rows."""
    result: dict[str, list[Candle]] = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or ())
        timestamp_key = next((key for key in ("timestamp", "ts", "start") if key in headers), None)
        if timestamp_key is None or not {"open", "high", "low", "close", "volume"} <= headers:
            raise ValueError("CSV requires timestamp (or ts/start), open, high, low, close, volume")
        if product_id is None and "product_id" not in headers:
            raise ValueError("CSV requires product_id or an explicit single-asset product_id")
        for line, row in enumerate(reader, 2):
            try:
                asset = (row.get("product_id") or product_id or "").strip()
                if product_id is not None and asset != product_id:
                    raise ValueError("row product_id conflicts with supplied product_id")
                candle = Candle(
                    ts=_timestamp(row[timestamp_key]),
                    **{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")},
                )
                result.setdefault(asset, []).append(candle)
            except (ValueError, TypeError, OverflowError) as exc:
                raise ValueError(f"CSV line {line}: {exc}") from exc
    if not result:
        raise ValueError("CSV contains no candles")
    for asset, candles in result.items():
        _validate_candles(asset, candles)
    return result


def _benchmark(candles: Sequence[Candle], config: BacktestConfig) -> dict:
    # Passive entry uses only the first open; no signal/approval or failure model.
    # Fees and price impact match the strategy. Terminal holdings are marked, unsold.
    price = candles[0].open * (1 + (config.spread_bps / 2 + config.slippage_bps) / 10_000)
    notional = config.initial_cash_usd / (1 + config.fee_bps / 10_000)
    size = notional / price
    equity = size * candles[-1].close
    peak = config.initial_cash_usd
    drawdown = 0.0
    for candle in candles:
        value = size * candle.close
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak)
    return {
        "initial_cash_usd": config.initial_cash_usd,
        "equity_usd": equity, "pnl_usd": equity - config.initial_cash_usd,
        "return_pct": (equity / config.initial_cash_usd - 1) * 100,
        "cash_usd": 0.0, "holdings": size,
        "fees_usd": notional * config.fee_bps / 10_000,
        "max_drawdown_pct": drawdown * 100,
        "entry_at": candles[0].ts.isoformat(), "entry_price": price,
    }


def backtest(
    product_id: str,
    candles: Sequence[Candle],
    *,
    parameters: StrategyParameters | None = None,
    config: BacktestConfig | None = None,
    start: int = 0,
    end: int | None = None,
    signal_fn: Callable[..., Proposal | None] | None = None,
) -> dict:
    """Simulate one spot account over [start, end), returning JSON-ready data.

    Signals see only completed candles before each open. Approval occurs after
    the configured latency; fills use the first available bar open at/after it.
    There is at most one pending order. BUY specifies quote notional (fee extra),
    SELL specifies base size at signal time. Cash/inventory cap actual fills;
    all unfilled remainders are canceled, with no shorting or borrowed cash.
    Average-cost realized PnL includes allocated entry fees and exit fees.
    Terminal holdings are marked to close, without an invented liquidation.
    """
    _validate_candles(product_id, candles)
    parameters = parameters or StrategyParameters()
    config = config or BacktestConfig()
    signal_fn = signal_fn or mean_reversion_signal
    end = len(candles) if end is None else end
    _integer(start, "start", 0)
    _integer(end, "end")
    if not start < end <= len(candles):
        raise ValueError("require 0 <= start < end <= candle count")
    cash = config.initial_cash_usd
    holdings = basis = realized = fees = impact_cost = 0.0
    orders: list[dict] = []
    curve: list[dict] = []
    pending: dict | None = None
    peak = cash
    max_drawdown = 0.0
    fee_rate = config.fee_bps / 10_000
    impact = (config.spread_bps / 2 + config.slippage_bps) / 10_000
    edge_filtered = 0

    for index in range(start, end):
        bar = candles[index]
        if pending is None:
            # Exclude the current bar altogether: neither its close, extremes nor
            # eventual volume may influence an order at its opening timestamp.
            history = list(candles[max(0, index - parameters.lookback):index])
            proposal = signal_fn(
                product_id, history, **asdict(parameters), now=bar.ts,
                granularity_seconds=INTERVAL_SECONDS,
            )
            if proposal is not None:
                _positive(proposal.signal_price, "signal_price")
                _positive(proposal.notional_usd, "proposal notional_usd")
                if proposal.product_id != product_id or proposal.side not in (Side.BUY, Side.SELL):
                    raise ValueError("signal returned an invalid product or side")
                edge = proposal.meta.get("estimated_edge_bps", proposal.meta.get("edge_bps"))
                if edge is not None:
                    _positive(float(edge), "estimated_edge_bps", zero=True)
                threshold = 2 * config.fee_bps + config.spread_bps + 2 * config.slippage_bps
                if proposal.side == Side.BUY and config.min_edge_buffer_bps is not None and (
                    edge is None or float(edge) <= threshold + config.min_edge_buffer_bps
                ):
                    edge_filtered += 1
                else:
                    pending = {
                        "order_id": len(orders) + 1, "side": proposal.side.value,
                        "signal_at": bar.ts.isoformat(),
                        "signal_candle_at": history[-1].ts.isoformat() if history else None,
                        "eligible_at": (bar.ts + timedelta(seconds=config.approval_latency_seconds)).isoformat(),
                        "signal_price": proposal.signal_price,
                        "estimated_edge_bps": edge,
                        "requested_notional_usd": proposal.notional_usd,
                        "status": "pending", "size": 0.0, "fee_usd": 0.0,
                    }
                    orders.append(pending)

        if pending is not None and bar.ts >= datetime.fromisoformat(pending["eligible_at"]):
            order = pending
            pending = None
            order["execution_at"] = bar.ts.isoformat()
            number = order["order_id"]
            if config.reject_every and number % config.reject_every == 0:
                order.update(status="rejected", reason="simulated_rejection")
            else:
                buy = order["side"] == Side.BUY.value
                price = bar.open * (1 + impact if buy else 1 - impact)
                requested = order["requested_notional_usd"] / (price if buy else order["signal_price"])
                fraction = config.partial_fraction if config.partial_every and number % config.partial_every == 0 else 1.0
                capacity = cash / (price * (1 + fee_rate)) if buy else holdings
                size = min(requested * fraction, max(0.0, capacity))
                order.update(price=price, reference_price=bar.open, requested_size=requested)
                if size <= 0:
                    order.update(status="rejected", reason="insufficient_cash" if buy else "no_inventory")
                else:
                    notional = size * price
                    fee = notional * fee_rate
                    trade_pnl = 0.0
                    if buy:
                        cash = max(0.0, cash - notional - fee)
                        holdings += size
                        basis += notional + fee
                    else:
                        removed_basis = basis * (size / holdings)
                        trade_pnl = notional - fee - removed_basis
                        realized += trade_pnl
                        cash += notional - fee
                        holdings = max(0.0, holdings - size)
                        basis = max(0.0, basis - removed_basis)
                    fees += fee
                    impact_cost += size * abs(price - bar.open)
                    order.update(
                        status="partial" if size < requested * (1 - 1e-12) else "filled",
                        size=size, notional_usd=notional, fee_usd=fee,
                        unfilled_size=max(0.0, requested - size),
                        remainder_policy="cancel", realized_pnl_usd=trade_pnl,
                        cash_after_usd=cash, holdings_after=holdings,
                        cost_basis_after_usd=basis,
                    )
        equity = cash + holdings * bar.close
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        curve.append({
            "at": (bar.ts + INTERVAL).isoformat(), "cash_usd": cash,
            "holdings": holdings, "mark_price": bar.close, "equity_usd": equity,
        })

    if pending is not None:
        pending.update(status="unfilled", reason="window_ended_before_execution")
    equity = curve[-1]["equity_usd"]
    unrealized = holdings * candles[end - 1].close - basis
    benchmark = _benchmark(candles[start:end], config)
    return {
        "product_id": product_id, "parameters": asdict(parameters), "config": asdict(config),
        "start_at": candles[start].ts.isoformat(), "end_at": (candles[end - 1].ts + INTERVAL).isoformat(),
        "bars": end - start, "warmup_bars": min(start, parameters.lookback),
        "initial_cash_usd": config.initial_cash_usd, "cash_usd": cash,
        "holdings": holdings, "cost_basis_usd": basis, "equity_usd": equity,
        "pnl_usd": equity - config.initial_cash_usd,
        "realized_pnl_usd": realized, "unrealized_pnl_usd": unrealized,
        "return_pct": (equity / config.initial_cash_usd - 1) * 100,
        "fees_usd": fees, "spread_slippage_cost_usd": impact_cost,
        "max_drawdown_pct": max_drawdown * 100,
        "order_count": len(orders),
        "fill_count": sum(o["status"] in ("filled", "partial") for o in orders),
        "partial_count": sum(o["status"] == "partial" for o in orders),
        "rejected_count": sum(o["status"] == "rejected" for o in orders),
        "unfilled_count": sum(o["status"] == "unfilled" for o in orders),
        "edge_filtered_count": edge_filtered,
        "benchmarks": {"buy_and_hold": benchmark, "cash": {
            "equity_usd": config.initial_cash_usd, "pnl_usd": 0.0, "return_pct": 0.0,
        }},
        "excess_return_pct": (equity - benchmark["equity_usd"]) / config.initial_cash_usd * 100,
        "orders": orders, "equity_curve": curve,
        "validation_status": "unvalidated_hypothesis",
    }


def _aggregate(results: Sequence[dict]) -> dict:
    capital = sum(r["initial_cash_usd"] for r in results)
    pnl = sum(r["pnl_usd"] for r in results)
    benchmark_pnl = sum(r["benchmarks"]["buy_and_hold"]["pnl_usd"] for r in results)
    return {
        "evaluation_count": len(results), "allocated_capital_usd": capital,
        "pnl_usd": pnl, "return_pct": pnl / capital * 100,
        "buy_and_hold_pnl_usd": benchmark_pnl,
        "buy_and_hold_return_pct": benchmark_pnl / capital * 100,
        "excess_return_pct": (pnl - benchmark_pnl) / capital * 100,
        "cash_return_pct": 0.0,
        "fees_usd": sum(r["fees_usd"] for r in results),
        "fill_count": sum(r["fill_count"] for r in results),
        "worst_window_drawdown_pct": max(r["max_drawdown_pct"] for r in results),
    }


def run_research(
    candles_by_asset: Mapping[str, Sequence[Candle]],
    *,
    parameters: Sequence[StrategyParameters] | None = None,
    config: BacktestConfig | None = None,
    train_bars: int = 672,
    test_bars: int = 96,
    step_bars: int | None = None,
    walk_forward: bool = True,
) -> dict:
    """Select parameters on rolling training windows, then evaluate later tests.

    Per-asset selection maximizes training return, breaking ties by grid order.
    Test windows cannot overlap. Accounts reset for every asset/window; aggregate
    returns are capital-weighted independent evaluations, never compounded.
    Full train/test sensitivity is reported but only train-selected test results
    enter the headline aggregate. Incomplete trailing windows are disclosed.
    """
    config = config or BacktestConfig()
    grid = list(parameters) if parameters is not None else [
        StrategyParameters(lookback=lookback, z_entry=z)
        for lookback, z in product((10, 20, 30), (1.0, 1.5, 2.0))
    ]
    if not grid or not all(isinstance(p, StrategyParameters) for p in grid):
        raise ValueError("parameters must be a nonempty sequence of StrategyParameters")
    _integer(train_bars, "train_bars")
    _integer(test_bars, "test_bars")
    step_bars = test_bars if step_bars is None else step_bars
    _integer(step_bars, "step_bars")
    if step_bars < test_bars:
        raise ValueError("step_bars must be >= test_bars to avoid overlapping tests")
    if train_bars <= max(p.lookback for p in grid):
        raise ValueError("train_bars must exceed every parameter lookback")
    if not candles_by_asset:
        raise ValueError("at least one asset is required")
    assets = sorted(candles_by_asset)
    for asset in assets:
        _validate_candles(asset, candles_by_asset[asset])
    reference = [c.ts for c in candles_by_asset[assets[0]]]
    if any([c.ts for c in candles_by_asset[a]] != reference for a in assets[1:]):
        raise ValueError("assets must have identical timestamps and coverage")
    count = len(reference)
    if count < train_bars + test_bars:
        raise ValueError("insufficient candles for a complete train/test window")
    starts = list(range(0, count - train_bars - test_bars + 1, step_bars)) if walk_forward else [0]
    per_asset: dict[str, dict] = {}
    all_selected: list[dict] = []
    for asset in assets:
        candles = candles_by_asset[asset]
        windows: list[dict] = []
        selected_results: list[dict] = []
        for number, start in enumerate(starts):
            split, end = start + train_bars, start + train_bars + test_bars
            # Equal warmup allowance makes training comparisons cover exactly
            # the same timestamps for every lookback. No test data used here.
            train_start = start + max(p.lookback for p in grid)
            training = [backtest(asset, candles, parameters=p, config=config, start=train_start, end=split) for p in grid]
            best = max(range(len(grid)), key=lambda i: training[i]["return_pct"])
            testing = [backtest(asset, candles, parameters=p, config=config, start=split, end=end) for p in grid]
            selected_results.append(testing[best])
            windows.append({
                "window": number + 1,
                "train_start_at": candles[start].ts.isoformat(),
                "train_evaluation_start_at": candles[train_start].ts.isoformat(),
                "test_start_at": candles[split].ts.isoformat(),
                "test_end_at": (candles[end - 1].ts + INTERVAL).isoformat(),
                "selected_parameter_index": best, "selected_parameters": asdict(grid[best]),
                "selected_test": testing[best],
                "sensitivity": [{"parameters": asdict(p), "train": train, "test": test}
                                for p, train, test in zip(grid, training, testing)],
            })
        per_asset[asset] = {"windows": windows, "aggregate": _aggregate(selected_results)}
        all_selected.extend(selected_results)
    fingerprint = hashlib.sha256()
    for asset in assets:
        for c in candles_by_asset[asset]:
            fingerprint.update(json.dumps([asset, c.ts.astimezone(timezone.utc).isoformat(),
                                           c.open, c.high, c.low, c.close, c.volume],
                                          separators=(",", ":"), allow_nan=False).encode())
    return {
        "schema_version": 1, "validation_status": "unvalidated_hypothesis",
        "method": "walk_forward" if walk_forward else "train_test",
        "config": asdict(config), "parameter_grid": [asdict(p) for p in grid],
        "train_bars": train_bars, "test_bars": test_bars, "step_bars": step_bars,
        "window_count": len(starts), "assets": per_asset,
        "aggregate": _aggregate(all_selected),
        "data": {"sha256": fingerprint.hexdigest(), "bars_per_asset": count,
                 "start_at": reference[0].isoformat(), "end_at": (reference[-1] + INTERVAL).isoformat(),
                 "unused_trailing_bars": count - (starts[-1] + train_bars + test_bars),
                 "unevaluated_bars_between_tests": max(0, len(starts) - 1) * (step_bars - test_bars)},
        "assumptions": [
            "15-minute timestamps are interval starts; signals use completed candles only.",
            "Execution uses the first next-bar open at/after signal availability plus approval latency.",
            "Fixed taker fee, half spread and adverse slippage are paid on actual filled notional.",
            "One pending order per asset; deterministic rejection takes precedence over partial fill.",
            "BUY quote notional excludes fees; SELL base size is fixed using the signal price.",
            "Optional distance-to-mean cost filter applies to BUY only; inventory exits are not edge-filtered.",
            "Cash/inventory cap fills; no shorting, leverage, or reinvested borrowed funds; remainders canceled.",
            "Independent cash account per asset/window; aggregate is capital-weighted, not compounded.",
            "Training reserves the largest lookback for warmup; testing uses prior candles only for warmup.",
            "Parameters selected by training return, ties resolved by input grid order.",
            "Buy-and-hold buys at the first evaluation open with identical price costs and fees; no approval/rejection model.",
            "Terminal inventory is marked at last close; no forced liquidation or exit fee is assumed.",
            "Drawdown uses initial cash and bar-close equity; intrabar drawdown is not observable.",
        ],
        "limitations": [
            "Unvalidated hypothesis; gross distance to mean is not an expected return or evidence of profitability.",
            "Test sensitivity is exploratory multiple testing; only training-selected tests enter aggregate.",
            "Synthetic execution failures, latency and fixed costs do not reconstruct historical liquidity or human approval.",
            "Candle data cannot establish intrabar execution quality; no volume-based fill claims are made.",
            "Exchange minimum sizes, precision, taxes and funding of cash are not modeled.",
            "Later rolling training windows may include earlier test history, as would chronological retraining.",
        ],
    }


def format_summary(report: dict) -> str:
    """Human-readable companion to the full JSON report."""
    lines = [f"UNVALIDATED HYPOTHESIS | {report['method']} | {report['window_count']} window(s)",
             "Independent accounts; returns are not compounded. Terminal holdings remain marked, unsold."]
    for label, result in [(asset, data["aggregate"]) for asset, data in report["assets"].items()] + [("ALL", report["aggregate"])]:
        lines.append(
            f"{label}: PnL ${result['pnl_usd']:.4f} | return {result['return_pct']:.2f}% | "
            f"buy/hold {result['buy_and_hold_return_pct']:.2f}% | cash 0.00% | "
            f"fees ${result['fees_usd']:.4f} | fills {result['fill_count']} | "
            f"worst-window drawdown {result['worst_window_drawdown_pct']:.2f}%"
        )
    lines.append("Edge = gross distance to mean; not a promised expectation. Test sensitivity is exploratory.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> dict:
    """CLI adapter; returns the same report dictionary it serializes.

    Repeated --csv accepts combined files or PRODUCT=path single-asset files.
    JSON goes to stdout (or --output); the summary goes to stderr.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", action="append", required=True, help="path or BTC-USD=path; repeat for separate assets")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--train-bars", type=int, default=672)
    parser.add_argument("--test-bars", type=int, default=96)
    parser.add_argument("--step-bars", type=int)
    parser.add_argument("--train-test", action="store_true", help="one chronological split instead of rolling windows")
    parser.add_argument("--lookbacks", default="10,20,30")
    parser.add_argument("--z-entries", default="1,1.5,2")
    parser.add_argument("--notional-usd", type=float, default=5.0)
    parser.add_argument("--initial-cash-usd", type=float, default=10.0)
    parser.add_argument("--fee-bps", type=float, default=60.0)
    parser.add_argument("--spread-bps", type=float, default=4.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--approval-latency-seconds", type=float, default=60.0)
    parser.add_argument("--reject-every", type=int, default=0)
    parser.add_argument("--partial-every", type=int, default=0)
    parser.add_argument("--partial-fraction", type=float, default=0.5)
    parser.add_argument("--min-edge-buffer-bps", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        data: dict[str, list[Candle]] = {}
        for entry in args.csv:
            asset, separator, path = entry.partition("=")
            if separator and asset in ASSETS:
                loaded = load_candles_csv(path, product_id=asset)
            else:
                loaded = load_candles_csv(entry)
            if data.keys() & loaded.keys():
                raise ValueError("an asset occurs in multiple CSV files; supply one chronological file per asset")
            data.update(loaded)
        config = BacktestConfig(**{name: getattr(args, name) for name in BacktestConfig.__dataclass_fields__})
        grid = [StrategyParameters(int(lookback), float(z), args.notional_usd)
                for lookback, z in product(args.lookbacks.split(","), args.z_entries.split(","))]
        report = run_research(data, parameters=grid, config=config, train_bars=args.train_bars,
                              test_bars=args.test_bars, step_bars=args.step_bars,
                              walk_forward=not args.train_test)
        serialized = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output is not None:
            args.output.write_text(serialized, encoding="utf-8")
        else:
            print(serialized, end="")
        print(format_summary(report), file=sys.stderr)
        return report
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
