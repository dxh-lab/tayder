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
) -> Fill:
    if proposal.side == Side.BUY:
        price = book.ask
    else:
        price = book.bid
    if price <= 0:
        raise ValueError("invalid_book_price")
    size = stake_usd / price
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
