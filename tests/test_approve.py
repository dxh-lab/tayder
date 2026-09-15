"""Approval state machine: double-click, expire, late click."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tayder.approve.state import ApprovalError, ApprovalStore
from tayder.models import Proposal, ProposalStatus, Side, utcnow


def _prop(**kwargs) -> Proposal:
    defaults = dict(
        product_id="BTC-USD",
        side=Side.BUY,
        notional_usd=5.0,
        reason="test",
        signal_price=100.0,
    )
    defaults.update(kwargs)
    return Proposal(**defaults)


def test_register_unique_proposal_id():
    store = ApprovalStore(default_ttl_seconds=60)
    p = _prop()
    store.register(p)
    with pytest.raises(ApprovalError) as ei:
        store.register(p)
    assert ei.value.code == "duplicate_proposal_id"


def test_approve_idempotent_double_click():
    store = ApprovalStore(default_ttl_seconds=60)
    p = store.register(_prop())
    a1 = store.approve(p.proposal_id)
    a2 = store.approve(p.proposal_id)
    assert a1.status == ProposalStatus.APPROVED
    assert a2.status == ProposalStatus.APPROVED
    assert a1 is a2


def test_skip_idempotent():
    store = ApprovalStore(default_ttl_seconds=60)
    p = store.register(_prop())
    s1 = store.skip(p.proposal_id)
    s2 = store.skip(p.proposal_id)
    assert s1.status == ProposalStatus.SKIPPED
    assert s2.status == ProposalStatus.SKIPPED


def test_expire_then_late_approve():
    store = ApprovalStore(default_ttl_seconds=1)
    now = utcnow()
    p = _prop()
    p.expires_at = now + timedelta(seconds=10)
    store.register(p, ttl_seconds=10)
    # advance past expiry
    late = now + timedelta(seconds=30)
    expired = store.expire_due(now=late)
    assert len(expired) == 1
    assert store.get(p.proposal_id).status == ProposalStatus.EXPIRED
    with pytest.raises(ApprovalError) as ei:
        store.approve(p.proposal_id, now=late)
    assert ei.value.code == "expired"


def test_late_click_without_expire_due():
    """approve() itself expires pending proposals that are past TTL."""
    store = ApprovalStore(default_ttl_seconds=1)
    now = utcnow()
    p = _prop()
    p.expires_at = now - timedelta(seconds=1)
    store.register(p, ttl_seconds=1)
    # force status pending with past expiry (register may have overwritten expires)
    p2 = store.get(p.proposal_id)
    assert p2 is not None
    p2.expires_at = now - timedelta(seconds=5)
    p2.status = ProposalStatus.PENDING
    with pytest.raises(ApprovalError) as ei:
        store.approve(p.proposal_id, now=now)
    assert ei.value.code == "expired"
    assert store.get(p.proposal_id).status == ProposalStatus.EXPIRED


def test_cannot_approve_after_skip():
    store = ApprovalStore(default_ttl_seconds=60)
    p = store.register(_prop())
    store.skip(p.proposal_id)
    with pytest.raises(ApprovalError) as ei:
        store.approve(p.proposal_id)
    assert ei.value.code == "already_skipped"


def test_mark_executed_requires_approved():
    store = ApprovalStore(default_ttl_seconds=60)
    p = store.register(_prop())
    with pytest.raises(ApprovalError):
        store.mark_executed(p.proposal_id)
    store.approve(p.proposal_id)
    e = store.mark_executed(p.proposal_id)
    assert e.status == ProposalStatus.EXECUTED
    # idempotent
    assert store.mark_executed(p.proposal_id).status == ProposalStatus.EXECUTED
