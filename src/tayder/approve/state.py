"""Proposal approval state machine — idempotent, unique proposal_id, expiry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock

from tayder.models import Proposal, ProposalStatus, utcnow


class ApprovalError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass
class ApprovalStore:
    """In-memory store; journal persists separately."""

    _by_id: dict[str, Proposal] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    default_ttl_seconds: int = 300

    def register(self, proposal: Proposal, ttl_seconds: int | None = None) -> Proposal:
        with self._lock:
            if proposal.proposal_id in self._by_id:
                raise ApprovalError("duplicate_proposal_id")
            ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
            if proposal.expires_at is None:
                proposal.expires_at = utcnow() + timedelta(seconds=ttl)
            proposal.status = ProposalStatus.PENDING
            self._by_id[proposal.proposal_id] = proposal
            return proposal

    def get(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            return self._by_id.get(proposal_id)

    def _expire_if_needed(self, p: Proposal, now: datetime) -> Proposal:
        if p.status == ProposalStatus.PENDING and p.is_expired(now):
            p.status = ProposalStatus.EXPIRED
        return p

    def approve(self, proposal_id: str, *, now: datetime | None = None) -> Proposal:
        """Idempotent approve. Double-click returns same approved state."""
        now = now or utcnow()
        with self._lock:
            p = self._by_id.get(proposal_id)
            if p is None:
                raise ApprovalError("not_found")
            self._expire_if_needed(p, now)
            if p.status == ProposalStatus.APPROVED:
                return p  # idempotent
            if p.status == ProposalStatus.EXPIRED:
                raise ApprovalError("expired")
            if p.status == ProposalStatus.SKIPPED:
                raise ApprovalError("already_skipped")
            if p.status in (ProposalStatus.EXECUTED, ProposalStatus.FAILED):
                raise ApprovalError("terminal")
            if p.status != ProposalStatus.PENDING:
                raise ApprovalError("invalid_state")
            p.status = ProposalStatus.APPROVED
            return p

    def skip(self, proposal_id: str, *, now: datetime | None = None) -> Proposal:
        now = now or utcnow()
        with self._lock:
            p = self._by_id.get(proposal_id)
            if p is None:
                raise ApprovalError("not_found")
            self._expire_if_needed(p, now)
            if p.status == ProposalStatus.SKIPPED:
                return p
            if p.status == ProposalStatus.EXPIRED:
                raise ApprovalError("expired")
            if p.status == ProposalStatus.APPROVED:
                raise ApprovalError("already_approved")
            if p.status in (ProposalStatus.EXECUTED, ProposalStatus.FAILED):
                raise ApprovalError("terminal")
            if p.status != ProposalStatus.PENDING:
                raise ApprovalError("invalid_state")
            p.status = ProposalStatus.SKIPPED
            return p

    def mark_executed(self, proposal_id: str) -> Proposal:
        with self._lock:
            p = self._by_id.get(proposal_id)
            if p is None:
                raise ApprovalError("not_found")
            if p.status == ProposalStatus.EXECUTED:
                return p
            if p.status != ProposalStatus.APPROVED:
                raise ApprovalError("not_approved")
            p.status = ProposalStatus.EXECUTED
            return p

    def mark_failed(self, proposal_id: str) -> Proposal:
        with self._lock:
            p = self._by_id.get(proposal_id)
            if p is None:
                raise ApprovalError("not_found")
            if p.status == ProposalStatus.FAILED:
                return p
            if p.status not in (ProposalStatus.APPROVED, ProposalStatus.PENDING):
                raise ApprovalError("invalid_state")
            p.status = ProposalStatus.FAILED
            return p

    def expire_due(self, *, now: datetime | None = None) -> list[Proposal]:
        now = now or utcnow()
        expired: list[Proposal] = []
        with self._lock:
            for p in self._by_id.values():
                before = p.status
                self._expire_if_needed(p, now)
                if before == ProposalStatus.PENDING and p.status == ProposalStatus.EXPIRED:
                    expired.append(p)
        return expired
