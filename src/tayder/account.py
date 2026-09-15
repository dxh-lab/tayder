"""The strategy's cash and per-product inventory, rebuilt from durable state.

Only this strategy's confirmed fills affect its budget. Unrelated Coinbase
holdings and deposits are never imported as trading capital.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import isfinite

from tayder.models import Fill, Side


@dataclass
class Position:
    size: float = 0.0
    cost_usd: float = 0.0


@dataclass
class Account:
    cash_usd: float
    positions: dict[str, Position] = field(default_factory=dict)
    last_trade_at: datetime | None = None
    realized_by_day: dict[str, float] = field(default_factory=dict)
    killed: bool = False

    def to_dict(self) -> dict:
        return {**asdict(self), "last_trade_at": self.last_trade_at.isoformat() if self.last_trade_at else None}

    @classmethod
    def from_dict(cls, data: dict) -> Account:
        result = cls(cash_usd=data["cash_usd"],
                   positions={k: Position(**v) for k, v in data["positions"].items()},
                   last_trade_at=datetime.fromisoformat(data["last_trade_at"]) if data["last_trade_at"] else None,
                   realized_by_day=dict(data["realized_by_day"]), killed=data["killed"])
        if not isfinite(result.cash_usd) or type(result.killed) is not bool:
            raise ValueError("Invalid persisted account")
        if any(k not in ("BTC-USD", "ETH-USD") or not isfinite(p.size)
               or p.size <= 0 or not isfinite(p.cost_usd) or p.cost_usd < 0
               for k, p in result.positions.items()):
            raise ValueError("Invalid persisted position")
        if any(not isfinite(v) for v in result.realized_by_day.values()):
            raise ValueError("Invalid persisted realized PnL")
        if result.last_trade_at and result.last_trade_at.tzinfo is None:
            raise ValueError("Invalid persisted trade time")
        return result

    def equity(self, marks: dict[str, float]) -> float:
        # Missing marks must never silently value an open position at zero.
        return self.cash_usd + sum(p.size * marks[t] for t, p in self.positions.items())

    def apply(self, fill: Fill) -> None:
        if (fill.side not in (Side.BUY, Side.SELL) or fill.product_id not in ("BTC-USD", "ETH-USD")
                or fill.filled_at.tzinfo is None):
            raise ValueError("Invalid confirmed fill identity or time")
        values = (fill.price, fill.size, fill.notional_usd, fill.fee_usd)
        if not all(isfinite(v) for v in values) or min(values[:3]) <= 0 or fill.fee_usd < 0:
            raise ValueError("Invalid confirmed fill")
        if abs(fill.price * fill.size - fill.notional_usd) > max(0.000001, fill.notional_usd * 0.00001):
            raise ValueError("Inconsistent confirmed fill")
        day = fill.filled_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        realized = -fill.fee_usd
        if fill.side == Side.BUY:
            p = self.positions.setdefault(fill.product_id, Position())
            p.size += fill.size
            p.cost_usd += fill.notional_usd
            self.cash_usd -= fill.notional_usd + fill.fee_usd
        else:
            p = self.positions.get(fill.product_id)
            if p is None or fill.size > p.size + 1e-12:
                raise ValueError("Confirmed sell exceeds strategy inventory")
            fraction = min(1.0, fill.size / p.size)
            basis = p.cost_usd * fraction
            realized += fill.notional_usd - basis
            self.cash_usd += fill.notional_usd - fill.fee_usd
            p.size -= fill.size
            p.cost_usd -= basis
            if p.size <= 1e-12:
                del self.positions[fill.product_id]
        self.realized_by_day[day] = self.realized_by_day.get(day, 0.0) + realized
        self.last_trade_at = max(filter(None, (self.last_trade_at, fill.filled_at)))
        if self.cash_usd < -1e-8 or len(self.positions) > 1:
            # Record what actually happened, then stop instead of hiding a breach.
            self.killed = True
