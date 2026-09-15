"""Runtime configuration from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    discord_token: str = ""
    discord_channel_id: int = 0
    discord_allowlist: frozenset[int] = field(default_factory=frozenset)
    mode: str = "paper"  # paper | live
    bankroll_usd: float = 10.0
    max_open_positions: int = 1
    min_notional_usd: float = 1.0
    cooldown_seconds: int = 300
    daily_loss_stop_pct: float = 0.25
    fee_dominance_bps: float = 20.0
    taker_fee_bps: float = 60.0
    maker_fee_bps: float = 25.0
    proposal_expiry_seconds: int = 300
    strategy_pairs: tuple[str, ...] = ("BTC-USD", "ETH-USD")
    candle_granularity_seconds: int = 900
    poll_interval_seconds: int = 60
    journal_db_path: str = "./data/tayder.db"
    coinbase_api_key_name: str = ""
    coinbase_api_private_key: str = ""
    coinbase_api_key_file: str = ""
    coinbase_api_base: str = "https://api.coinbase.com"
    killed: bool = False  # runtime kill-switch (mutable via object.replace)

    @property
    def is_live(self) -> bool:
        return self.mode.lower() == "live"

    def private_key_pem(self) -> str:
        if self.coinbase_api_key_file:
            return Path(self.coinbase_api_key_file).read_text()
        key = self.coinbase_api_private_key
        return key.replace("\\n", "\n") if key else ""


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file)
    allow = _csv("DISCORD_ALLOWLIST_USER_IDS")
    pairs = _csv("STRATEGY_PAIRS", "BTC-USD,ETH-USD")
    channel = os.getenv("DISCORD_CHANNEL_ID", "0") or "0"
    bankroll = min(_f("BANKROLL_USD", 10.0), 10.0)
    return Settings(
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        discord_channel_id=int(channel),
        discord_allowlist=frozenset(int(x) for x in allow),
        mode=os.getenv("MODE", "paper").lower(),
        bankroll_usd=bankroll,
        max_open_positions=_i("MAX_OPEN_POSITIONS", 1),
        min_notional_usd=_f("MIN_NOTIONAL_USD", 1.0),
        cooldown_seconds=_i("COOLDOWN_SECONDS", 300),
        daily_loss_stop_pct=_f("DAILY_LOSS_STOP_PCT", 0.25),
        fee_dominance_bps=_f("FEE_DOMINANCE_BPS", 20.0),
        taker_fee_bps=_f("TAKER_FEE_BPS", 60.0),
        maker_fee_bps=_f("MAKER_FEE_BPS", 25.0),
        proposal_expiry_seconds=_i("PROPOSAL_EXPIRY_SECONDS", 300),
        strategy_pairs=tuple(pairs),
        candle_granularity_seconds=_i("CANDLE_GRANULARITY_SECONDS", 900),
        poll_interval_seconds=_i("POLL_INTERVAL_SECONDS", 60),
        journal_db_path=os.getenv("JOURNAL_DB_PATH", "./data/tayder.db"),
        coinbase_api_key_name=os.getenv("COINBASE_API_KEY_NAME", ""),
        coinbase_api_private_key=os.getenv("COINBASE_API_PRIVATE_KEY", ""),
        coinbase_api_key_file=os.getenv("COINBASE_API_KEY_FILE", ""),
        coinbase_api_base=os.getenv("COINBASE_API_BASE", "https://api.coinbase.com"),
    )
