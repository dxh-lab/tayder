"""Durable approval transitions; an approval is not an execution claim."""
from __future__ import annotations

from datetime import datetime, timedelta
from threading import RLock

from tayder.models import Proposal, ProposalStatus as S, utcnow


class ApprovalError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class ApprovalStore:
    def __init__(self, default_ttl_seconds: int = 300, journal=None, *, lock=None, clock=None) -> None:
        self.now = clock or utcnow
        self.default_ttl_seconds = default_ttl_seconds
        self.journal = journal
        self._lock = lock or RLock()
        self._by_id = {p.proposal_id: p for p in journal.proposals()} if journal else {}

    def _save(self, p: Proposal) -> Proposal:
        if self.journal:
            self.journal.save_proposal(p)
        self._by_id[p.proposal_id] = p
        return p

    def register(self, proposal: Proposal, ttl_seconds: int | None = None) -> Proposal:
        with self._lock:
            if proposal.proposal_id in self._by_id:
                raise ApprovalError("duplicate_proposal_id")
            if proposal.expires_at is None:
                proposal.expires_at = self.now() + timedelta(seconds=ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds)
            proposal.status = S.PENDING
            return self._save(proposal)

    def get(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            return self._by_id.get(proposal_id)

    def active(self) -> list[Proposal]:
        with self._lock:
            return [p for p in self._by_id.values() if p.status in (S.PENDING, S.APPROVED, S.SUBMITTING)]

    def all_proposals(self) -> list[Proposal]:
        with self._lock:
            return list(self._by_id.values())

    def approve(self, proposal_id: str, *, now: datetime | None = None) -> Proposal:
        with self._lock:
            p = self._require(proposal_id)
            self._expire(p, now or self.now())
            if p.status == S.APPROVED:
                return p
            if p.status != S.PENDING:
                raise ApprovalError("expired" if p.status == S.EXPIRED else "already_skipped" if p.status == S.SKIPPED else "terminal")
            p.status = S.APPROVED
            return self._save(p)

    def skip(self, proposal_id: str, *, now: datetime | None = None) -> Proposal:
        with self._lock:
            p = self._require(proposal_id)
            self._expire(p, now or self.now())
            if p.status == S.SKIPPED:
                return p
            if p.status != S.PENDING:
                raise ApprovalError("expired" if p.status == S.EXPIRED else "already_approved" if p.status == S.APPROVED else "terminal")
            p.status = S.SKIPPED
            return self._save(p)

    def _require(self, pid: str) -> Proposal:
        p = self.get(pid)
        if p is None:
            raise ApprovalError("not_found")
        return p

    def _expire(self, p: Proposal, now: datetime) -> None:
        if p.status in (S.PENDING, S.APPROVED) and p.is_expired(now):
            p.status = S.EXPIRED
            self._save(p)

    def transition(self, pid: str, expected: tuple[S, ...], status: S, **meta) -> Proposal:
        with self._lock:
            p = self._require(pid)
            if p.status not in expected:
                raise ApprovalError("invalid_state")
            p.status = status
            p.meta.update(meta)
            return self._save(p)

    def mark_executed(self, pid: str) -> Proposal:
        return self.transition(pid, (S.APPROVED, S.SUBMITTING, S.EXECUTED), S.EXECUTED)

    def mark_failed(self, pid: str) -> Proposal:
        return self.transition(pid, (S.PENDING, S.APPROVED, S.SUBMITTING, S.FAILED), S.FAILED)

    def expire_due(self, *, now: datetime | None = None) -> list[Proposal]:
        with self._lock:
            expired = []
            for p in self.active():
                before = p.status
                self._expire(p, now or self.now())
                if before != p.status:
                    expired.append(p)
            return expired
