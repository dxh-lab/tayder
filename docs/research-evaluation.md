# Q1 2025 research evaluation

The baseline has no demonstrated profitable edge in this evaluation. These are
independent signal/execution simulations, not a replay of the complete Discord
worker or a compounded portfolio.

## Data and method

- Evaluated on 2026-09-15 from Coinbase Exchange public BTC-USD and ETH-USD candles.
- Coverage: 2025-01-01 through 2025-04-01 exclusive; 8,640 closed 15-minute bars per asset.
- Data fingerprint: `215478e234805f86e361b59d887cc70aa483e0e7c83e3b71f032686c46ea42f2`.
- Rolling 14-day training / 7-day test windows; ten nonoverlapping test windows per asset.
- Lookbacks 10, 20, 30; entry z-scores 1.0, 1.5, 2.0. Each window selects by training return only.
- Each evaluation starts with $10 cash and requests $5 trades. Aggregate denominator is $200 across 20 independent evaluations.
- Last 576 bars per asset are disclosed but unused because a full next test window would not fit.
- Cash benchmark earns 0%; buy-and-hold pays the matching entry fee and price impact. Terminal holdings are marked, unsold.

## Results

| Scenario | Asset | Strategy return | Buy-and-hold | Fees | Fills | Worst window drawdown |
|---|---|---:|---:|---:|---:|---:|
| baseline | BTC-USD | -2.62% | -1.58% | $1.7858 | 72 | 11.96% |
| baseline | ETH-USD | -6.26% | -4.81% | $2.7059 | 108 | 21.41% |
| baseline | Combined | -4.44% | -3.19% | $4.4917 | 180 | 21.41% |
| stress | BTC-USD | -0.71% | -2.04% | $0.6494 | 19 | 6.43% |
| stress | ETH-USD | -3.30% | -5.26% | $1.4500 | 43 | 12.80% |
| stress | Combined | -2.00% | -3.65% | $2.0994 | 62 | 12.80% |

Baseline: 60 bps fee per side, 4 bps full spread, 10 bps adverse slippage per
side, 60-second approval latency, no synthetic order failures. Stress: 100 bps
fee, 20 bps spread, 10 bps slippage, 900-second latency, every fifth order rejected
and every third eligible order half-filled. Both use a 20 bps entry cost buffer.

The stress case trades less and loses less here; that is not a general claim
that higher costs improve returns. The cost screen and training selection alter
which orders are attempted. Both scenarios lose against cash. Parameter
sensitivity is exploratory and must not be used to pick a winner after seeing
the test data.

The harness allows repeated entries until cash is exhausted and does not replay
the runtime one-position rule, cooldown, daily-loss stop, quote-drift checks,
expiry or a shared BTC/ETH bankroll. Fees and liquidity are assumptions, not
reconstructed historical execution. One quarter cannot validate performance
across future regimes.

## Reproduce

Use the export and two research commands in the [README](../README.md#research).
The original CSV and complete order/equity-curve reports are kept in the ignored
`data/research/` directory. [Compact results](research-evaluation.json) preserve
the source, CSV checksum, input fingerprint, assumptions, parameters, every
selected test window and full parameter-return sensitivity.

The Advanced Trade public endpoint returned recent candles outside the requested
historical ranges during this run. Export validation rejected those responses.
The evaluation explicitly used the [Coinbase Exchange history endpoint](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles);
the worker still uses Advanced Trade for current quotes and closed candles.
