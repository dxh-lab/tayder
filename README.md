# tayder

Hands-off **crypto SPOT** helper: strategy proposes → **Discord Approve/Skip** → Coinbase Advanced Trade.

- **Spot only.** No leverage, no futures, no margin.
- **PAPER mode by default.** LIVE is optional and explicit (`MODE=live`).
- **Bankroll ≤ $10.** Hard-capped in config.
- **Pairs:** BTC-USD, ETH-USD.
- **Never auto-trades** without a Discord Approve click.
- Kill-switch: Discord `/kill` (resume with `/resume`).

## Honesty

This is a small helper, not a money printer. A simple 15m mean-reversion baseline will not mint 10k× returns. Retail spot fees often dominate thin edges. Expect to lose money; size is capped so you cannot lose more than the bankroll you configured (≤ $10). Past candles ≠ future PnL.

## Security

- Coinbase CDP API key: grant **view + trade only**. **Never** enable transfer / withdraw.
- Keep `COINBASE_API_PRIVATE_KEY` out of git; use `.env` or `COINBASE_API_KEY_FILE`.
- Restrict Discord actions with `DISCORD_ALLOWLIST_USER_IDS`.
- PAPER is the default; LIVE will refuse to start orders if credentials are missing.

## Setup

```bash
cd tayder
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# fill DISCORD_TOKEN, DISCORD_CHANNEL_ID, allowlist…
```

### Discord bot

1. Create an application + bot; enable Message Content Intent if you use prefix commands (slash commands work with default intents + `applications.commands`).
2. Invite with `applications.commands` and `Send Messages` / `Embed Links` in your channel.
3. Put the bot token and channel snowflake in `.env`.

### Coinbase (LIVE only)

Create a CDP **secret** API key (ES256) with trade permission, no transfer. Set:

- `COINBASE_API_KEY_NAME` — full resource name (`organizations/.../apiKeys/...`)
- `COINBASE_API_PRIVATE_KEY` — PEM with `\n` escapes, **or**
- `COINBASE_API_KEY_FILE` — path to PEM file

Auth implements URI-bound short-lived ES256 JWTs per Coinbase CDP docs.

## Run

```bash
# default paper loop (requires Discord)
tayder
# or
python -m tayder

# one-shot public-market scan without Discord
python -m tayder --dry-scan
```

Docker:

```bash
cp .env.example .env   # edit
docker compose up --build -d
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

No real Discord/Coinbase keys required for unit tests (approve state machine + risk gates + JWT shape).

## Layout

```
src/tayder/
  data/       # public candles + top-of-book (httpx)
  strategy/   # mean-reversion → Proposal
  risk/       # bankroll, open, cooldown, daily loss, fees
  notify/     # Discord embed + Approve/Skip + /kill
  approve/    # pending|approved|skipped|expired|executed|failed
  execute/    # paper fill + live REST + CDP JWT
  journal/    # sqlite
  worker.py   # loop
```

## Modules (behavior)

| Gate | Rule |
|------|------|
| Bankroll | stake ≤ bankroll ≤ $10 |
| Open | max 1; new BUYs blocked when open |
| Min notional | reject dust |
| Cooldown | seconds since last fill |
| Daily loss | ~25% of bankroll realized |
| Fees | refuse when expected edge ≤ RT fees + buffer |
| Approve | unique `proposal_id`, idempotent Approve, expiry rejects late clicks |
