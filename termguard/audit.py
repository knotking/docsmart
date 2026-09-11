"""Layer S (substrate): the append-only audit log.

Constraint 4. Every consequential act writes exactly one :class:`AuditEvent`. Nothing in
this module updates or deletes; ``record`` only ever inserts.

The exportable trail (``audit_csv``) joins changes to their latest decision so a QA reader
gets one row per proposed edit with its provenance and outcome - which is the artifact
that gets asked for in an audit.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable, Sequence

from sqlmodel import Session, select

from termguard.models import (
    ActorKind,
    AuditEvent,
    Change,
    Decision,
    Document,
    DocumentVersion,
    Hit,
    Mechanism,
)


def record(
    session: Session,
    event: str,
    *,
    summary: str = "",
    actor: str = "system",
    actor_kind: ActorKind = ActorKind.SYSTEM,
    run_id: int | None = None,
    document_id: int | None = None,
    document_version_id: int | None = None,
    hit_id: int | None = None,
    change_id: int | None = None,
    mechanism: Mechanism | None = None,
    rule_id: str | None = None,
    content_sha256: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one event. Returns the flushed row so callers can reference its id."""
    ev = AuditEvent(
        event=event,
        summary=summary,
        actor=actor,
        actor_kind=actor_kind,
        run_id=run_id,
        document_id=document_id,
        document_version_id=document_version_id,
        hit_id=hit_id,
        change_id=change_id,
        mechanism=mechanism,
        rule_id=rule_id,
        content_sha256=content_sha256,
        payload=payload or {},
    )
    session.add(ev)
    session.flush()
    return ev


def events_for_run(session: Session, run_id: int) -> Sequence[AuditEvent]:
    """Every event for a run, in insertion order."""
    return session.exec(
        select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.id)
    ).all()


def events_for_document(session: Session, document_id: int) -> Sequence[AuditEvent]:
    """Every event touching one document, across all runs - the per-file history."""
    return session.exec(
        select(AuditEvent).where(AuditEvent.document_id == document_id).order_by(AuditEvent.id)
    ).all()


def latest_decision(session: Session, change_id: int) -> Decision | None:
    """The newest decision for a change. Older rows remain as history."""
    return session.exec(
        select(Decision)
        .where(Decision.change_id == change_id)
        .order_by(Decision.decided_at.desc(), Decision.id.desc())  # type: ignore[union-attr]
    ).first()


AUDIT_COLUMNS = [
    "change_id", "run_id", "document", "doc_version", "version_stage", "content_sha256",
    "part", "location", "paragraph_index", "rule_id", "classification", "mechanism",
    "original_text", "proposed_text", "model", "prompt_version", "prompt_hash",
    "request_id", "llm_decision", "justification", "proposed_at",
    "decision", "reviewer", "final_text", "decided_at", "note",
]


def audit_rows(session: Session, run_id: int) -> list[dict[str, Any]]:
    """One row per change, with its latest decision and full provenance."""
    changes = session.exec(
        select(Change).where(Change.run_id == run_id).order_by(Change.id)
    ).all()

    rows: list[dict[str, Any]] = []
    for ch in changes:
        hit = session.get(Hit, ch.hit_id)
        doc = session.get(Document, ch.document_id)
        ver = session.get(DocumentVersion, ch.document_version_id) if ch.document_version_id else None
        dec = latest_decision(session, ch.id) if ch.id else None
        loc = hit.location if hit else {}
        rows.append(
            {
                "change_id": ch.id,
                "run_id": ch.run_id,
                "document": doc.name if doc else "",
                "doc_version": ver.version_no if ver else "",
                "version_stage": ver.stage.value if ver else "",
                "content_sha256": ver.content_sha256 if ver else "",
                "part": hit.part if hit else "",
                "location": _format_location(loc),
                "paragraph_index": hit.paragraph_index if hit else "",
                "rule_id": hit.rule_id if hit else "",
                "classification": hit.classification.value if hit else "",
                "mechanism": ch.mechanism.value,
                "original_text": ch.original_text,
                "proposed_text": ch.proposed_text,
                "model": ch.model or "",
                "prompt_version": ch.prompt_version or "",
                "prompt_hash": ch.prompt_hash or "",
                "request_id": ch.request_id or "",
                "llm_decision": ch.llm_decision or "",
                "justification": ch.justification or "",
                "proposed_at": _iso(ch.created_at),
                "decision": dec.decision.value if dec else "undecided",
                "reviewer": dec.reviewer if dec else "",
                "final_text": (dec.final_text or "") if dec else "",
                "decided_at": _iso(dec.decided_at) if dec else "",
                "note": (dec.note or "") if dec else "",
            }
        )
    return rows


def audit_csv(session: Session, run_id: int) -> str:
    """The audit trail as CSV text."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=AUDIT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(audit_rows(session, run_id))
    return buf.getvalue()


def _format_location(loc: dict[str, Any]) -> str:
    """Human-readable location, e.g. 'table 2 / row 3 / cell 1' or 'header1.xml'."""
    if not loc:
        return ""
    if loc.get("container_path"):
        return str(loc["container_path"])
    for key in ("part_name", "part"):
        if loc.get(key):
            return str(loc[key])
    return ""


def _iso(value: Any) -> str:
    return value.isoformat() if value is not None else ""
