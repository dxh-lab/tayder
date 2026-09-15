"""Temporal, execution and accounting invariants for offline research."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tayder.models import Candle, Proposal, Side
from tayder.research import (
    BacktestConfig,
    StrategyParameters,
    backtest,
    format_summary,
    load_candles_csv,
    main,
    run_research,
)
from tayder.strategy.baseline import mean_reversion_signal

BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)
PARAMS = StrategyParameters(lookback=2, z_entry=0.5, notional_usd=50)


def candles(closes, opens=None):
    opens = closes if opens is None else opens
    return [Candle(BASE + i * STEP, float(o), float(max(o, c)), float(min(o, c)), float(c), 10.0)
            for i, (o, c) in enumerate(zip(opens, closes))]


def config(**overrides):
    values = dict(initial_cash_usd=100, fee_bps=0, spread_bps=0,
                  slippage_bps=0, approval_latency_seconds=0, min_edge_buffer_bps=None)
    values.update(overrides)
    return BacktestConfig(**values)


def schedule(events, *, edge=1000.0):
    """Events map bar index to (side, quote notional, decision reference price)."""
    def signal(asset, history, *, now, **kwargs):
        assert all(c.ts + STEP <= now for c in history)
        index = int((now - BASE) / STEP)
        event = events.get(index)
        if event is None:
            return None
        side, notional, price = event
        return Proposal(asset, side, notional, "test", price,
                        created_at=now, meta={"estimated_edge_bps": edge})
    return signal


def simulate(data, events, **kwargs):
    return backtest("BTC-USD", data, parameters=PARAMS,
                    signal_fn=schedule(events), **kwargs)


def test_baseline_uses_all_closed_rows_by_default_and_labels_edge():
    data = candles([100, 100, 80])
    proposal = mean_reversion_signal("BTC-USD", data, lookback=3, z_entry=1)
    assert proposal.side == Side.BUY
    assert proposal.signal_price == 80
    assert proposal.meta["direction"] == "BUY"
    assert proposal.meta["estimated_edge_bps"] == pytest.approx(((280 / 3) - 80) / 80 * 10000)
    assert proposal.meta["edge_bps"] == proposal.meta["estimated_edge_bps"]
    assert proposal.meta["edge_basis"] == "gross_distance_to_mean"
    assert proposal.meta["validation_status"] == "unvalidated_hypothesis"


def test_baseline_filters_forming_future_and_observes_exact_boundary():
    data = candles([100, 100, 80, 1000, 1])
    assert mean_reversion_signal("BTC-USD", data, lookback=3, z_entry=1,
                                 now=BASE + 3 * STEP - timedelta(microseconds=1)) is None
    proposal = mean_reversion_signal("BTC-USD", data, lookback=3, z_entry=1, now=BASE + 3 * STEP)
    assert proposal.side == Side.BUY
    assert proposal.signal_price == 80
    assert proposal.created_at == BASE + 3 * STEP
    assert proposal.meta["signal_available_at"] == (BASE + 3 * STEP).isoformat()


def test_baseline_sell_and_custom_granularity():
    data = [replace(c, ts=BASE + timedelta(minutes=i)) for i, c in enumerate(candles([100, 100, 120]))]
    proposal = mean_reversion_signal("ETH-USD", data, lookback=3, z_entry=1,
                                    now=BASE + timedelta(minutes=3), granularity_seconds=60)
    assert proposal.side == Side.SELL
    assert proposal.meta["direction"] == "SELL"
    assert proposal.meta["estimated_edge_bps"] > 0


def test_baseline_short_constant_or_gapped_history_has_no_signal():
    assert mean_reversion_signal("BTC-USD", candles([100]), lookback=2) is None
    assert mean_reversion_signal("BTC-USD", candles([100] * 3), lookback=3) is None
    data = candles([100, 90, 80, 70])
    assert mean_reversion_signal("BTC-USD", [data[0], data[2], data[3]], lookback=3) is None


@pytest.mark.parametrize("kwargs", [{"lookback": 1}, {"lookback": True}, {"z_entry": 0},
                                    {"z_entry": math.nan}, {"notional_usd": -1},
                                    {"granularity_seconds": 0}, {"now": datetime(2025, 1, 1)}])
def test_baseline_rejects_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        mean_reversion_signal("BTC-USD", candles([100, 90, 80]), **kwargs)


def test_real_baseline_executes_at_next_open_not_signal_close():
    data = candles([100, 100, 80, 90], opens=[100, 100, 80, 120])
    result = backtest("BTC-USD", data, parameters=StrategyParameters(3, 1, 50), config=config())
    order = result["orders"][0]
    assert order["signal_candle_at"] == data[2].ts.isoformat()
    assert order["execution_at"] == data[3].ts.isoformat()
    assert order["signal_price"] == 80
    assert order["price"] == 120
    assert result["holdings"] == pytest.approx(50 / 120)
    assert result["equity_usd"] == pytest.approx(50 + 50 / 120 * 90)


@pytest.mark.parametrize("latency,execution_index", [(0, 1), (1, 2), (900, 2), (901, 3), (1800, 3)])
def test_latency_uses_first_eligible_open(latency, execution_index):
    data = candles([10] * 5, opens=[10, 11, 12, 13, 14])
    result = simulate(data, {1: (Side.BUY, 50, 10)}, config=config(approval_latency_seconds=latency))
    order = result["orders"][0]
    assert order["execution_at"] == data[execution_index].ts.isoformat()
    assert order["price"] == data[execution_index].open
    assert datetime.fromisoformat(order["execution_at"]) >= datetime.fromisoformat(order["eligible_at"])


def test_pending_order_at_end_never_fills_and_does_not_reserve_cash():
    result = simulate(candles([10, 10]), {1: (Side.BUY, 50, 10)},
                      config=config(approval_latency_seconds=1))
    assert result["orders"][0]["status"] == "unfilled"
    assert result["cash_usd"] == 100
    assert result["holdings"] == result["fees_usd"] == result["pnl_usd"] == 0


def test_only_one_pending_order_and_decision_is_frozen_during_latency():
    data = candles([10] * 5)
    events = {i: (Side.BUY if i == 1 else Side.SELL, 50, 10) for i in range(1, 5)}
    result = simulate(data, events, config=config(approval_latency_seconds=1800))
    assert [o["signal_at"] for o in result["orders"]] == [data[1].ts.isoformat(), data[4].ts.isoformat()]
    assert result["orders"][0]["side"] == "BUY"
    assert result["orders"][0]["execution_at"] == data[3].ts.isoformat()
    assert result["orders"][1]["status"] == "unfilled"


def test_roundtrip_realized_pnl_includes_both_fees():
    result = simulate(candles([10, 10, 12]),
                      {1: (Side.BUY, 50, 10), 2: (Side.SELL, 50, 10)}, config=config(fee_bps=100))
    assert result["cash_usd"] == pytest.approx(108.9)
    assert result["holdings"] == result["cost_basis_usd"] == 0
    assert result["fees_usd"] == pytest.approx(1.1)
    assert result["realized_pnl_usd"] == pytest.approx(8.9)
    assert result["pnl_usd"] == pytest.approx(8.9)
    assert result["unrealized_pnl_usd"] == 0


def test_sell_uses_fixed_base_size_and_current_execution_price():
    result = simulate(candles([10, 10, 12]),
                      {1: (Side.BUY, 50, 10), 2: (Side.SELL, 20, 10)}, config=config(fee_bps=100))
    assert result["orders"][1]["size"] == 2
    assert result["orders"][1]["notional_usd"] == 24
    assert result["holdings"] == 3
    assert result["cost_basis_usd"] == pytest.approx(30.3)
    assert result["realized_pnl_usd"] == pytest.approx(3.56)
    assert result["unrealized_pnl_usd"] == pytest.approx(5.7)
    assert result["pnl_usd"] == pytest.approx(9.26)


def test_spread_slippage_are_adverse_and_fees_use_actual_notional():
    result = simulate(candles([100, 100, 100]),
                      {1: (Side.BUY, 50, 100), 2: (Side.SELL, 100, 100)},
                      config=config(fee_bps=100, spread_bps=20, slippage_bps=10))
    buy, sell = result["orders"]
    assert buy["price"] == pytest.approx(100.2)
    assert sell["price"] == pytest.approx(99.8)
    assert buy["fee_usd"] == pytest.approx(0.5)
    assert sell["fee_usd"] == pytest.approx(sell["size"] * 99.8 * 0.01)
    assert result["spread_slippage_cost_usd"] == pytest.approx(buy["size"] * 0.4)
    assert result["pnl_usd"] == pytest.approx(-result["fees_usd"] - result["spread_slippage_cost_usd"])


def test_buy_is_capped_by_cash_including_fee():
    result = simulate(candles([10, 10]), {1: (Side.BUY, 1000, 10)}, config=config(fee_bps=100))
    assert result["orders"][0]["status"] == "partial"
    assert result["cash_usd"] == pytest.approx(0, abs=1e-12)
    assert result["holdings"] == pytest.approx(100 / 10.1)
    assert result["cost_basis_usd"] == pytest.approx(100)
    assert result["pnl_usd"] == pytest.approx(-result["fees_usd"])


def test_sell_cannot_create_cash_without_inventory():
    result = simulate(candles([10] * 3), {1: (Side.SELL, 50, 10), 2: (Side.SELL, 50, 10)}, config=config())
    assert result["cash_usd"] == 100
    assert result["holdings"] == result["pnl_usd"] == result["fees_usd"] == 0
    assert result["rejected_count"] == 2
    assert {o["reason"] for o in result["orders"]} == {"no_inventory"}


def test_partial_buy_and_sell_remainders_are_canceled():
    result = simulate(candles([10, 10, 12]),
                      {1: (Side.BUY, 50, 10), 2: (Side.SELL, 20, 10)},
                      config=config(fee_bps=100, partial_every=1, partial_fraction=0.5))
    buy, sell = result["orders"]
    assert buy["size"] == 2.5
    assert sell["size"] == 1
    assert buy["unfilled_size"] == 2.5
    assert sell["unfilled_size"] == 1
    assert result["partial_count"] == 2
    assert result["cash_usd"] == pytest.approx(86.63)
    assert result["holdings"] == 1.5
    assert result["realized_pnl_usd"] == pytest.approx(1.78)
    assert result["unrealized_pnl_usd"] == pytest.approx(2.85)
    assert result["pnl_usd"] == pytest.approx(4.63)


def test_rejection_precedes_partial_is_deterministic_and_charges_nothing():
    data = candles([10] * 5)
    events = {i: (Side.BUY, 10, 10) for i in range(1, 5)}
    cfg = config(reject_every=2, partial_every=1, partial_fraction=0.5, fee_bps=100)
    a = simulate(data, events, config=cfg)
    b = simulate(data, events, config=cfg)
    assert a == b
    assert [o["status"] for o in a["orders"]] == ["partial", "rejected", "partial", "rejected"]
    assert a["holdings"] == 1
    assert a["cash_usd"] == pytest.approx(89.9)
    assert a["fees_usd"] == pytest.approx(0.1)


def test_pnl_identity_after_multiple_entries_exits_and_failures():
    data = candles([10, 11, 9, 13, 8, 14, 10, 12, 7, 15])
    events = {i: (Side.BUY if i % 3 else Side.SELL, 35, data[i - 1].close) for i in range(1, len(data))}
    result = simulate(data, events, config=config(fee_bps=60, spread_bps=10, slippage_bps=5,
                                                 partial_every=2, reject_every=4))
    assert result["pnl_usd"] == pytest.approx(result["realized_pnl_usd"] + result["unrealized_pnl_usd"])
    assert result["equity_usd"] == pytest.approx(result["cash_usd"] + result["holdings"] * data[-1].close)
    assert result["fees_usd"] == pytest.approx(sum(o["fee_usd"] for o in result["orders"]))
    assert all(p["cash_usd"] >= 0 and p["holdings"] >= 0 for p in result["equity_curve"])


def test_buy_edge_gate_accounts_for_roundtrip_costs_and_never_blocks_exits():
    data = candles([10] * 4)
    cfg = config(fee_bps=10, spread_bps=4, slippage_bps=3, min_edge_buffer_bps=5)
    # Threshold = 2*10 + 4 + 2*3 + 5 = 35 bps; equality does not pass.
    low = backtest("BTC-USD", data, parameters=PARAMS, config=cfg,
                   signal_fn=schedule({1: (Side.BUY, 50, 10)}, edge=35))
    assert low["edge_filtered_count"] == 1
    assert low["order_count"] == 0
    events = {1: (Side.BUY, 50, 10), 2: (Side.SELL, 50, 10)}
    buy_signal = schedule(events, edge=36)
    def signal(asset, history, **kwargs):
        proposal = buy_signal(asset, history, **kwargs)
        if proposal is not None and proposal.side == Side.SELL:
            proposal.meta["estimated_edge_bps"] = 0
        return proposal
    result = backtest("BTC-USD", data, parameters=PARAMS, config=cfg, signal_fn=signal)
    assert result["fill_count"] == 2
    assert result["holdings"] == 0
    assert result["edge_filtered_count"] == 0


def test_future_prices_and_current_close_never_change_prior_execution():
    data = candles([100, 100, 80, 90, 110, 80, 100])
    changed = list(data)
    changed[3:] = [replace(c, close=c.close * 10, high=max(c.open, c.close * 10)) for c in changed[3:]]
    kwargs = dict(parameters=StrategyParameters(3, 1, 50), config=config())
    original = backtest("BTC-USD", data, **kwargs)
    altered = backtest("BTC-USD", changed, **kwargs)
    assert original["orders"][0] == altered["orders"][0]
    assert original["equity_curve"][:3] == altered["equity_curve"][:3]


def test_signal_callback_receives_only_completed_history():
    seen = []
    data = candles([100, 90, 80, 100, 120])
    def spy(asset, history, *, now, **kwargs):
        seen.append((now, history))
        assert all(c.ts + STEP <= now for c in history)
        assert len(history) <= 2
        return None
    backtest("ETH-USD", data, parameters=PARAMS, config=config(), signal_fn=spy)
    assert len(seen) == len(data)
    assert seen[0][1] == []
    assert seen[-1][1] == data[-3:-1]


def test_buy_and_hold_costs_and_terminal_marking():
    result = simulate(candles([10, 12]), {}, config=config(fee_bps=100, spread_bps=20, slippage_bps=10))
    benchmark = result["benchmarks"]["buy_and_hold"]
    assert benchmark["entry_price"] == pytest.approx(10.02)
    assert benchmark["holdings"] == pytest.approx(100 / 1.01 / 10.02)
    assert benchmark["equity_usd"] == pytest.approx(benchmark["holdings"] * 12)
    assert benchmark["fees_usd"] == pytest.approx(100 / 1.01 * 0.01)
    assert result["benchmarks"]["cash"]["return_pct"] == 0


def test_drawdown_includes_initial_cash_before_first_fill_fee():
    result = simulate(candles([10, 10, 5, 10]), {1: (Side.BUY, 1000, 10)}, config=config(fee_bps=100))
    assert result["max_drawdown_pct"] == pytest.approx((100 - 100 / 1.01 / 2) / 100 * 100)
    assert result["holdings"] > 0  # no invented final sale


@pytest.mark.parametrize("override", [
    {"initial_cash_usd": 0}, {"fee_bps": -1}, {"fee_bps": math.nan}, {"fee_bps": 10000},
    {"slippage_bps": 10000}, {"spread_bps": math.inf}, {"approval_latency_seconds": -1},
    {"reject_every": -1}, {"reject_every": 1.5}, {"partial_every": True},
    {"partial_fraction": 0}, {"partial_fraction": 1.1}, {"min_edge_buffer_bps": -1},
])
def test_invalid_execution_config(override):
    with pytest.raises(ValueError):
        BacktestConfig(**override)


@pytest.mark.parametrize("change", [
    lambda cs: list(reversed(cs)),
    lambda cs: [cs[0], cs[0]],
    lambda cs: [cs[0], replace(cs[1], ts=cs[1].ts + STEP)],
    lambda cs: [replace(c, ts=c.ts + timedelta(seconds=1)) for c in cs],
    lambda cs: [replace(c, ts=c.ts.replace(tzinfo=None)) for c in cs],
    lambda cs: [replace(cs[0], close=math.nan), cs[1]],
    lambda cs: [replace(cs[0], low=101), cs[1]],
    lambda cs: [replace(cs[0], high=99), cs[1]],
    lambda cs: [replace(cs[0], volume=-1), cs[1]],
    lambda cs: [replace(cs[0], open=0), cs[1]],
])
def test_invalid_candle_data_is_rejected(change):
    with pytest.raises(ValueError):
        backtest("BTC-USD", change(candles([100, 100])))


def write_csv(path, data, *, asset="BTC-USD", timestamp_key="timestamp", include_asset=True):
    lines = [("product_id," if include_asset else "") + f"{timestamp_key},open,high,low,close,volume"]
    for candle in data:
        ts = str(int(candle.ts.timestamp())) if timestamp_key == "start" else candle.ts.isoformat()
        lines.append((asset + "," if include_asset else "") +
                     f"{ts},{candle.open},{candle.high},{candle.low},{candle.close},{candle.volume}")
    path.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("key", ["timestamp", "ts", "start"])
def test_csv_timestamp_formats_and_single_asset(tmp_path, key):
    path = tmp_path / "data.csv"
    data = candles([100, 90, 110])
    write_csv(path, data, timestamp_key=key, include_asset=False)
    assert load_candles_csv(path, "ETH-USD") == {"ETH-USD": data}


def test_csv_accepts_interleaved_assets_but_rejects_reordering(tmp_path):
    path = tmp_path / "data.csv"
    data = candles([100, 90])
    write_csv(path, data)
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[1], lines[1].replace("BTC", "ETH"),
                               lines[2], lines[2].replace("BTC", "ETH")]))
    loaded = load_candles_csv(path)
    assert loaded == {"BTC-USD": data, "ETH-USD": data}
    write_csv(path, list(reversed(data)))
    with pytest.raises(ValueError, match="chronological"):
        load_candles_csv(path)


@pytest.mark.parametrize("body", [
    "timestamp,close\n2025-01-01T00:00:00Z,1\n",
    "timestamp,open,high,low,close,volume\n2025-01-01T00:00:00Z,1,1,1,1,1\n",
    "product_id,timestamp,open,high,low,close,volume\n",
    "product_id,timestamp,open,high,low,close,volume\nBTC-USD,2025-01-01T00:00:00,1,1,1,1,1\n",
    "product_id,timestamp,open,high,low,close,volume\nBTC-USD,bad,1,1,1,1,1\n",
])
def test_csv_errors_are_clear(tmp_path, body):
    path = tmp_path / "bad.csv"
    path.write_text(body)
    with pytest.raises(ValueError):
        load_candles_csv(path)


def research_data(count=24):
    btc = candles(([100, 100, 80, 100, 120, 100] * ((count + 5) // 6))[:count])
    eth = [replace(c, open=c.open * 0.1, high=c.high * 0.1,
                   low=c.low * 0.1, close=c.close * 0.1) for c in btc]
    return {"BTC-USD": btc, "ETH-USD": eth}


GRID = [StrategyParameters(2, 0.5, 30), StrategyParameters(3, 1.1, 30)]


def test_walk_forward_selection_sensitivity_assets_and_json_determinism():
    data = research_data(25)
    kwargs = dict(parameters=GRID, config=config(reject_every=3, partial_every=2), train_bars=8, test_bars=4)
    report = run_research(data, **kwargs)
    assert report == run_research(data, **kwargs)
    assert json.loads(json.dumps(report, allow_nan=False)) == report
    assert report["window_count"] == 4
    assert report["data"]["unused_trailing_bars"] == 1
    assert len(report["data"]["sha256"]) == 64
    assert report["aggregate"]["evaluation_count"] == 8
    assert report["aggregate"]["allocated_capital_usd"] == 800
    for asset in report["assets"].values():
        previous_end = None
        for window in asset["windows"]:
            sensitivity = window["sensitivity"]
            selected = window["selected_parameter_index"]
            assert sensitivity[selected]["train"]["return_pct"] == max(s["train"]["return_pct"] for s in sensitivity)
            assert window["selected_test"] == sensitivity[selected]["test"]
            assert all(s["train"]["end_at"] == window["test_start_at"] for s in sensitivity)
            assert all(s["test"]["initial_cash_usd"] == 100 for s in sensitivity)
            if previous_end:
                assert window["test_start_at"] >= previous_end
            previous_end = window["test_end_at"]
    assert "UNVALIDATED HYPOTHESIS" in format_summary(report)
    assert "BTC-USD" in format_summary(report) and "ETH-USD" in format_summary(report)


def test_changing_test_data_cannot_change_training_or_selection():
    original = research_data(12)
    modified = {asset: list(rows) for asset, rows in original.items()}
    for asset, rows in modified.items():
        rows[8:] = [replace(c, open=c.open * 5, high=c.high * 5, low=c.low * 5, close=c.close * 5) for c in rows[8:]]
    kwargs = dict(parameters=GRID, config=config(), train_bars=8, test_bars=4, walk_forward=False)
    before, after = run_research(original, **kwargs), run_research(modified, **kwargs)
    for asset in original:
        a, b = before["assets"][asset]["windows"][0], after["assets"][asset]["windows"][0]
        assert a["selected_parameters"] == b["selected_parameters"]
        assert [s["train"] for s in a["sensitivity"]] == [s["train"] for s in b["sensitivity"]]


def test_walk_forward_prefix_cannot_change_with_future_extension():
    kwargs = dict(parameters=GRID, config=config(), train_bars=8, test_bars=4)
    prefix = run_research(research_data(16), **kwargs)
    full = run_research(research_data(24), **kwargs)
    for asset in prefix["assets"]:
        assert prefix["assets"][asset]["windows"] == full["assets"][asset]["windows"][:2]


def test_train_test_and_spaced_windows_disclose_unused_bars():
    kwargs = dict(parameters=GRID, config=config(), train_bars=8, test_bars=4)
    split = run_research(research_data(), walk_forward=False, **kwargs)
    assert split["window_count"] == 1
    assert split["data"]["unused_trailing_bars"] == 12
    rolling = run_research(research_data(), step_bars=6, **kwargs)
    assert rolling["window_count"] == 3
    assert rolling["data"]["unevaluated_bars_between_tests"] == 4


@pytest.mark.parametrize("kwargs", [
    {"train_bars": 2}, {"test_bars": 0}, {"step_bars": 1},
    {"train_bars": 100}, {"parameters": []}, {"train_bars": 3.5},
])
def test_invalid_research_windows_and_grid(kwargs):
    values = dict(parameters=GRID, train_bars=8, test_bars=4)
    values.update(kwargs)
    with pytest.raises(ValueError):
        run_research(research_data(), **values)


def test_mismatched_asset_coverage_is_rejected():
    data = research_data()
    data["ETH-USD"] = data["ETH-USD"][1:]
    with pytest.raises(ValueError, match="identical timestamps"):
        run_research(data, parameters=GRID, train_bars=8, test_bars=4)


def test_cli_json_stdout_summary_stderr_and_output_file(tmp_path, capsys):
    path = tmp_path / "btc.csv"
    write_csv(path, research_data(12)["BTC-USD"], include_asset=False)
    args = ["--csv", f"BTC-USD={path}", "--train-bars", "8", "--test-bars", "4",
            "--lookbacks", "2,3", "--z-entries", "0.5,1", "--train-test"]
    report = main(args)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == report
    assert "UNVALIDATED HYPOTHESIS" in captured.err
    destination = tmp_path / "report.json"
    assert main(args + ["--output", str(destination)]) == report
    assert json.loads(destination.read_text()) == report
    assert capsys.readouterr().out == ""


def test_cli_invalid_input_exits_with_usage_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["--csv", str(tmp_path / "missing.csv")])
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err
