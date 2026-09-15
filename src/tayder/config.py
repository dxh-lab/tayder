"""Runtime configuration from environment."""

from __future__ import annotations

import os
import math
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
    max_spread_bps: float = 50.0
    max_price_drift_bps: float = 50.0
    max_book_age_seconds: int = 60
    slippage_bps: float = 10.0

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError("MODE must be paper or live")
        if self.coinbase_api_base != "https://api.coinbase.com":
            raise ValueError("COINBASE_API_BASE must be https://api.coinbase.com")
        for name in ("bankroll_usd", "min_notional_usd", "taker_fee_bps", "maker_fee_bps",
                     "fee_dominance_bps", "daily_loss_stop_pct", "max_spread_bps",
                     "max_price_drift_bps", "slippage_bps"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid {name}")
        if not 0 < self.bankroll_usd <= 10:
            raise ValueError("bankroll must be positive and at most $10")
        if not 0 < self.min_notional_usd <= self.bankroll_usd:
            raise ValueError("Invalid min_notional_usd")
        if not 0 < self.daily_loss_stop_pct <= 1:
            raise ValueError("Invalid daily_loss_stop_pct")
        if self.max_open_positions != 1:
            raise ValueError("MAX_OPEN_POSITIONS must be 1")
        if self.taker_fee_bps >= 10_000 or self.slippage_bps >= 10_000:
            raise ValueError("Fee and slippage rates must be below 10000 bps")
        for name in ("cooldown_seconds", "proposal_expiry_seconds", "poll_interval_seconds",
                     "max_book_age_seconds", "max_open_positions", "candle_granularity_seconds"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if self.cooldown_seconds < 0 or min(self.proposal_expiry_seconds,
                self.poll_interval_seconds, self.max_book_age_seconds) <= 0:
            raise ValueError("Invalid time limits")
        if self.candle_granularity_seconds != 900:
            raise ValueError("The baseline requires 15-minute candles")
        if not self.strategy_pairs or len(set(self.strategy_pairs)) != len(self.strategy_pairs) or not set(self.strategy_pairs) <= {"BTC-USD", "ETH-USD"}:
            raise ValueError("STRATEGY_PAIRS must contain unique BTC-USD/ETH-USD spot pairs")
        if self.is_live:
            if not self.discord_allowlist or any(uid <= 0 for uid in self.discord_allowlist):
                raise ValueError("LIVE requires DISCORD_ALLOWLIST_USER_IDS")
            if not self.coinbase_api_key_name or not self.private_key_pem():
                raise ValueError("LIVE requires Coinbase credentials")

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
    bankroll = _f("BANKROLL_USD", 10.0)
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
        max_spread_bps=_f("MAX_SPREAD_BPS", 50),
        max_price_drift_bps=_f("MAX_PRICE_DRIFT_BPS", 50),
        max_book_age_seconds=_i("MAX_BOOK_AGE_SECONDS", 60),
        slippage_bps=_f("SLIPPAGE_BPS", 10),
    )
