"""Paper fills from top-of-book + fees."""

from __future__ import annotations

from uuid import uuid4

from tayder.models import Fill, Proposal, Side, TopOfBook


def paper_fill(
    proposal: Proposal,
    book: TopOfBook,
    *,
    stake_usd: float,
    taker_fee_bps: float,
    mode: str = "paper",
    base_size: float | None = None,
    slippage_bps: float = 0.0,
) -> Fill:
    if proposal.side == Side.BUY:
        price = book.ask * (1 + slippage_bps / 10_000)
    else:
        price = book.bid * (1 - slippage_bps / 10_000)
    if price <= 0:
        raise ValueError("invalid_book_price")
    size = stake_usd / price if proposal.side == Side.BUY else base_size
    if size is None or size <= 0:
        raise ValueError("sell_requires_owned_base_size")
    stake_usd = size * price
    fee = stake_usd * (taker_fee_bps / 10_000)
    return Fill(
        proposal_id=proposal.proposal_id,
        product_id=proposal.product_id,
        side=proposal.side,
        price=price,
        size=size,
        fee_usd=fee,
        notional_usd=stake_usd,
        mode=mode,
        order_id=f"paper-{uuid4()}",
    )
