"""Shared domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SKIPPED = "skipped"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class TopOfBook:
    product_id: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    ts: datetime = field(default_factory=utcnow)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        if self.mid <= 0:
            return 0.0
        return (self.ask - self.bid) / self.mid * 10_000


@dataclass
class Proposal:
    product_id: str
    side: Side
    notional_usd: float
    reason: str
    signal_price: float
    proposal_id: str = field(default_factory=lambda: str(uuid4()))
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or utcnow()) >= self.expires_at


@dataclass
class Fill:
    proposal_id: str
    product_id: str
    side: Side
    price: float
    size: float
    fee_usd: float
    notional_usd: float
    mode: str
    order_id: str
    filled_at: datetime = field(default_factory=utcnow)
