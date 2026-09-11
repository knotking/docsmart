"""Layer D, part 1: recording reviewer decisions.

Decisions are append-only (constraint 4): changing your mind inserts a new row rather than
editing the old one, so the trail shows that a decision was revised and by whom. The
"current" decision is simply the newest one for a change.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlmodel import Session, select

from termguard import audit
from termguard.models import (
    ActorKind,
    Change,
    ChangeStatus,
    Decision,
    DecisionKind,
    Document,
    Hit,
    Mechanism,
    Run,
)


def record_decision(
    session: Session,
    change_id: int,
    decision: DecisionKind | str,
    reviewer: str,
    *,
    final_text: str | None = None,
    note: str | None = None,
) -> Decision:
    """Record one reviewer decision. Never updates an existing row."""
    change = session.get(Change, change_id)
    if change is None:
        raise ValueError(f"no change {change_id}")

    kind = DecisionKind(decision) if isinstance(decision, str) else decision
    if kind is DecisionKind.EDITED and not (final_text or "").strip():
        raise ValueError("an 'edited' decision requires final_text")

    row = Decision(
        change_id=change_id,
        run_id=change.run_id,
        decision=kind,
        reviewer=reviewer,
        final_text=final_text,
        note=note,
    )
    session.add(row)
    session.flush()

    # The change's status is a cursor, not a rewrite of history.
    change.status = ChangeStatus.DECIDED
    session.add(change)

    hit = session.get(Hit, change.hit_id) if change.hit_id else None
    audit.record(
        session, "decision.recorded",
        summary=f"{reviewer} {kind.value} change {change_id}"
                + (f" ({hit.rule_id})" if hit else ""),
        actor=reviewer, actor_kind=ActorKind.HUMAN,
        run_id=change.run_id, document_id=change.document_id,
        document_version_id=change.document_version_id,
        hit_id=change.hit_id, change_id=change_id,
        mechanism=change.mechanism, rule_id=hit.rule_id if hit else None,
        payload={
            "decision": kind.value,
            "final_text": final_text,
            "note": note,
            "proposed": change.proposed_text,
            "original": change.original_text,
        },
    )
    return row


def pending_changes(session: Session, run_id: int) -> Sequence[Change]:
    """Changes with no decision yet. These are what the reviewer queue shows.

    Comment-only changes are included. When the judge recommends *keeping* a deprecated
    term, a reviewer still has to ratify that: accepting means "yes, leave it", and that
    ratification is what lets the verification gate treat the term the scanner will keep
    finding as adjudicated rather than outstanding.
    """
    decided = {
        row.change_id
        for row in session.exec(select(Decision).where(Decision.run_id == run_id)).all()
    }
    return [
        change
        for change in session.exec(
            select(Change).where(Change.run_id == run_id).order_by(Change.id)
        ).all()
        if change.id not in decided
    ]


def auto_accept_all(session: Session, run_id: int, reviewer: str = "auto-accept (demo)") -> int:
    """Accept every pending change. For the dry-run demo only - never a real review path."""
    count = 0
    for change in pending_changes(session, run_id):
        record_decision(session, change.id, DecisionKind.ACCEPTED, reviewer,  # type: ignore[arg-type]
                        note="auto-accepted in dry-run demo mode")
        count += 1
    session.commit()
    return count


def queue_item(session: Session, change: Change) -> dict[str, Any]:
    """One reviewer-queue entry: everything the reviewer needs to decide, in one payload."""
    hit = session.get(Hit, change.hit_id) if change.hit_id else None
    document = session.get(Document, change.document_id)
    latest = audit.latest_decision(session, change.id)  # type: ignore[arg-type]
    return {
        "change_id": change.id,
        "run_id": change.run_id,
        "file": document.name if document else "",
        "document_id": change.document_id,
        "mechanism": change.mechanism.value,
        "status": change.status.value,
        "rule_id": hit.rule_id if hit else "",
        "classification": hit.classification.value if hit else "",
        "reason": hit.reason if hit else "",
        "part": hit.part if hit else "",
        "location": hit.location.get("container_path") if hit else "",
        "paragraph_index": hit.paragraph_index if hit else 0,
        "is_heading": hit.is_heading if hit else False,
        "original_text": change.original_text,
        "proposed_text": change.proposed_text,
        "sentence": hit.sentence if hit else "",
        "paragraph_text": hit.paragraph_text if hit else "",
        "comment": change.comment,
        "model": change.model,
        "prompt_hash": change.prompt_hash,
        "llm_decision": change.llm_decision,
        "justification": change.justification,
        "decision": latest.decision.value if latest else None,
        "reviewer": latest.reviewer if latest else None,
        "final_text": latest.final_text if latest else None,
    }
