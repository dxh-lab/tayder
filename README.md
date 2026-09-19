# Tayder

A Discord-gated **BTC/ETH spot trading experiment** for Coinbase Advanced Trade.
The 15-minute mean-reversion strategy proposes a trade; an allowlisted user
approves or skips it. Paper mode is the default. The configured starting bankroll
must be positive: at most **$100** in paper and **$10** in live.

The strategy is an **unvalidated hypothesis**. Distance to a moving average is
not an expected return. Fees, spreads, delayed approvals and changing prices can
erase that distance. The software does not promise profitability or a guaranteed
maximum lifetime loss. See [research](#research) for reproducible evaluation.
The [Q1 2025 evaluation](docs/research-evaluation.md) records the first baseline
and stress results; both lost money across the independent test accounts.

## Setup

Requires Python 3.12+ and Linux (the worker uses a process lock).

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

Create a Discord application and bot, invite it with `bot` and
`applications.commands`, and allow Send Messages / Embed Links in its channel.
No privileged Message Content Intent is needed. Configure `DISCORD_TOKEN`,
`DISCORD_CHANNEL_ID`, and comma-separated `DISCORD_ALLOWLIST_USER_IDS` in `.env`.
An empty allowlist allows everyone in paper mode; live mode refuses an empty list.

```bash
.venv/bin/python -m tayder
# Isolated public-market paper scan: no Discord, no production journal writes
.venv/bin/python -m tayder --dry-scan
```

Docker retains the journal and lock in a named volume:

```bash
docker compose up --build -d
```

Run one worker per journal and one strategy owner per Coinbase portfolio. Do not
run several independent journals against the same bankroll. Back up the SQLite
database while the worker is stopped, or use SQLite's online backup API. Do not
copy only the main `.db` file from a running WAL database.

## Execution and accounting

1. Only completed, contiguous, recent candles can produce a signal. Quotes use
   Coinbase's server timestamp; missing, stale or crossed books are rejected.
2. Pending and approved proposals reserve cash **including estimated entry fees**
   and the single available position slot. A product cannot have overlapping
   proposals, including two exits against the same inventory. Skip and expiry
   release reservations; both decisions persist.
3. Each approval is looked up by its stored ID. Before execution, the worker
   checks kill state, expiry, cash, per-product inventory, open positions,
   cooldown, daily loss, spread, price movement and the cost filter again.
4. BUYs require an explicit distance-to-mean estimate greater than round-trip
   fees + full spread + two-sided slippage + the configured buffer when
   `ENFORCE_FEE_DOMINANCE` is on (live default). Paper defaults to advisory:
   the proposal still posts with a cost warning so you get an Approve/Skip
   action. Distance to mean is a cost screen, **not evidence of an edge**. The
   distance is recomputed against the original signal mean at the new
   executable price. Discord is notified only when a fresh z-entry cross
   clears the other gates — not on skipped scans or continuation bars.
5. SELLs use a frozen base quantity from strategy-owned inventory, capped by
   current exchange-available inventory in live mode. Unrelated Coinbase
   holdings never become strategy capital. Exits remain possible after the
   daily-loss stop or cooldown; the kill switch blocks exits too. Residual dust
   below minimum order size remains owned and keeps the position slot occupied.
6. A durable `submitting` intent is committed before any order POST. Its client
   order ID is the proposal ID. Repeated callbacks cannot submit it again.
7. Live orders are booked only after terminal settlement, using confirmed size,
   value, average price, fees and fill time. A canceled order with a partial fill
   books that actual fill; an open partial order remains reserved. There is no
   assumed fill at the signal price.
8. A fill, proposal completion and account update commit in one SQLite
   transaction. Cash, product inventory, cost basis, UTC daily P&L, cooldown,
   kill state and pending proposals survive restart. A lifetime process lock
   prevents two workers from owning the same journal.

Paper fills use current bid/ask plus configured adverse slippage and taker fees.
BUY size excludes the fee, and SELL proceeds follow the approved base size.
The daily-loss gate counts trade gains/losses and charges each fee on its fill's
UTC date. Unrealized changes do not count toward that gate. An actual fill that
leaves negative strategy cash or multiple positions is recorded and engages the
kill switch; estimates never replace the exchange's result.

Discord shows **strategy equity** (cash + holdings marked at current mid prices),
not the entire Coinbase account. The displayed percentage is stake / equity,
not a forecast of loss. Pending buttons have stable IDs and recover after restart.

### Commands and recovery

- `/kill`: persist the stop and invalidate pending/approved proposals. A request
  received during preflight is checked before POST. Orders already sent to
  Coinbase can still execute; their results continue to reconcile while killed.
- `/resume`: clear the stop. Invalidated proposals require a new proposal and
  approval; they are not revived.
- `/status`: current mode, configured bankroll, cash, kill state, position count
  and unresolved order count.

After a lost submission response or crash, the worker looks up the order by its
client order ID and reconciles it. **An empty listing does not prove rejection.**
Until the result is known, it blocks new execution and keeps the reservation.
It never retries an uncertain POST, including a crash between intent commit and
POST. Inspect the Coinbase order history and the journal's `submitting` proposal
in this case. Preserve the journal; do not delete it or relabel the order as
failed to force trading to restart. A permanently unresolvable intent requires
operator investigation before a journal repair.

A journal is bound to its mode, starting bankroll and live key name. Changing
those in place refuses startup (for example, moving paper from $10 to $100
needs a new `JOURNAL_DB_PATH` or a wiped paper journal). Keep paper and live
journals separate. Existing paper journals can replay valid fills; legacy live
journals containing estimated fills or unresolved proposals refuse automatic
migration and require comparison with actual Coinbase history first.

## Optional live mode

Set `MODE=live` only when you intend to use Coinbase trading. A nonempty Discord
allowlist and credentials are required. Create an ES256/P-256 CDP key with
**view + trade**, with transfer disabled. The executor verifies those permissions
before every POST, validates current USD SPOT product restrictions and rounds
sizes down to exchange increments/minimums. Available USD/base balances are
checked again immediately before POST.

Configure either a PEM key with escaped `\n` in `COINBASE_API_PRIVATE_KEY`, or a
readable PEM file through `COINBASE_API_KEY_FILE`, plus
`COINBASE_API_KEY_NAME=organizations/.../apiKeys/...`.

Only `https://api.coinbase.com` is accepted as the configured live origin. Signed
requests never follow redirects and ignore proxy environment variables. Keys,
`.env`, journals and downloaded market data are excluded from git.

REST integration uses Coinbase's documented
[Create Order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order)
and [Get Order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/get-order)
contracts. Offline tests exercise those contracts with mocked responses; they do
not certify a credentialed live account or place real orders.

## Research

Download public, fully closed 15-minute candles for both assets. This command
uses no account credentials, verifies complete identical coverage, paces bulk
GETs and retries HTTP 429 responses a bounded number of times.
`--source exchange` explicitly uses the public [Coinbase Exchange candle API](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles).
The default `advanced` source uses the Advanced Trade public endpoint. Neither
source silently substitutes recent candles for the requested historical range.
The worker continues to use Advanced Trade for current market data.

```bash
.venv/bin/python -m tayder.data.export --source exchange \
  --start 2025-01-01T00:00:00Z --end 2025-04-01T00:00:00Z \
  --output data/research/2025-q1.csv

.venv/bin/python -m tayder.research \
  --csv data/research/2025-q1.csv \
  --train-bars 1344 --test-bars 672 \
  --slippage-bps 10 \
  --output data/research/baseline.json
```

CSV columns are `product_id,timestamp,open,high,low,close,volume`. Timestamps are
interval starts, Unix seconds or timezone-aware ISO dates. `ts` and `start` are
also accepted timestamp headers. Use `--csv BTC-USD=btc.csv --csv ETH-USD=eth.csv`
for separate files lacking a product column. Gaps, duplicates, unordered rows,
invalid OHLC and mismatched asset coverage are rejected.

The deterministic simulator:

- Computes signals from completed candles only. Fills occur at the first bar
  open at or after signal availability plus approval latency, never at a close
  that was just used to calculate the signal.
- Models fixed taker fees, full bid/ask spread, adverse slippage, approval delay,
  deterministic rejections and partial fills with canceled remainders.
- Tracks cash, inventory, entry cost including fees, realized/unrealized P&L,
  equity, drawdown, fees and an order-by-order execution trace.
- Compares with cash and buy-and-hold under matching price/fee assumptions.
- Selects parameters using training results only, evaluates later nonoverlapping
  test windows, reports the full parameter sensitivity and separate BTC/ETH
  results. `--train-test` uses a single chronological split.
- Includes the full configuration, data SHA-256 fingerprint, coverage, discarded
  trailing bars and modeling assumptions in JSON. Summary text goes to stderr.

Stress example:

```bash
.venv/bin/python -m tayder.research \
  --csv data/research/2025-q1.csv \
  --train-bars 1344 --test-bars 672 \
  --fee-bps 100 --spread-bps 20 --slippage-bps 10 \
  --approval-latency-seconds 900 --reject-every 5 \
  --partial-every 3 --partial-fraction 0.5 \
  --output data/research/stress.json
```

Research accounts reset independently for each asset/window; aggregate results
are capital-weighted, not compounded and not a shared $10 portfolio replay.
The research harness permits repeated entries up to cash capacity and does not
replay the worker's position-count, cooldown, daily-loss, quote-drift or proposal
expiry gates. It evaluates signal/execution assumptions, not identical Discord
behavior. Its realized P&L allocates entry fees to exits; the worker's daily-loss
gate instead charges fees when incurred. Terminal holdings remain marked and
unsold. Exchange minimum sizes/precision, actual historical liquidity, intrabar
adverse selection, human decisions, taxes and a future market regime cannot be
inferred from candles. Test sensitivity is exploratory multiple testing and must
not be used to retroactively select the reported best strategy.

## Verification

```bash
.venv/bin/python -m pytest -q
```

Tests cover market validation/pagination, signed API contracts and endpoint
restrictions, durable approvals/reservations, per-product inventory, kill timing,
restart recovery, duplicate callbacks, ambiguous orders, partial fills, atomic
ledger rollback, Discord recovery and temporal/accounting research invariants.
No real Discord or Coinbase credentials are used by the tests.
