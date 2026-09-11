"""Layer A (rulebook), intake: source in, reviewed rules out.

The whole path, in order:

    upload / fetch  ->  read (sources.py)  ->  extract (extract.py)
                    ->  candidates, each quoting its source line
                    ->  a person accepts, edits or rejects
                    ->  the rule enters the rulebook, the hash moves, the run is redone

The step that is deliberately not automatic is the fourth. An extracted rule is not a
suggestion about wording, it is an instruction that will rewrite text across every
document in the corpus on the next run - and a reversed or over-broad one does not fail
loudly. It produces confident, wrong edits. So a candidate carries the sentence it came
from and the reviewer checks the sentence.

Accepting a rule is also the one action here that invalidates work already done: the
rulebook hash moves, and :mod:`termguard.verify` refuses to verify a run against a hash it
was not produced with. That is intended, and :func:`pending_reruns` says which runs it
affects so nobody has to notice on their own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlmodel import Session, func, select

from termguard import audit
from termguard.extract import Candidate, extract, next_rule_id
from termguard.models import (
    ActorKind,
    CandidateStatus,
    Run,
    RuleCandidate,
    RuleSource,
    SourceKind,
    utcnow,
)
from termguard.rulebook import Rule, RuleError, Rulebook, dump_rulebook, load_rulebook
from termguard.sources import ExtractedSource, SourceError, read_bytes, read_source, from_url
from termguard.storage import ObjectStore


class IntakeError(RuntimeError):
    """A source could not be taken in, or a candidate could not be applied."""


# ------------------------------------------------------------------ ingest


def _persist(
    session: Session,
    source: ExtractedSource,
    candidates: Sequence[Candidate],
    *,
    uploaded_by: str,
    blob_uri: str | None = None,
) -> RuleSource:
    row = RuleSource(
        name=source.name,
        kind=SourceKind(source.kind),
        origin=source.origin,
        content_sha256=source.content_sha256,
        blob_uri=blob_uri,
        uploaded_by=uploaded_by,
        retrieved_at=source.retrieved_at,
        lines_read=len(source.lines),
        candidates_found=len(candidates),
        needs=list(source.needs),
        notes=list(source.notes),
    )
    session.add(row)
    session.flush()

    for candidate in candidates:
        session.add(RuleCandidate(
            source_id=row.id,  # type: ignore[arg-type]
            deprecated=candidate.deprecated,
            approved=candidate.approved,
            method=candidate.method,
            confidence=candidate.confidence,
            quote=candidate.quote,
            locator=candidate.locator,
            note=candidate.note or None,
            warnings=list(candidate.warnings),
            suggested=dict(candidate.suggested),
        ))
    session.flush()

    audit.record(
        session, "source.ingested",
        summary=(
            f"{source.kind} {source.name!r}: {len(source.lines)} line(s) read, "
            f"{len(candidates)} candidate rule(s)"
            + (" - not fully readable" if source.needs else "")
        ),
        actor=uploaded_by, actor_kind=ActorKind.HUMAN,
        content_sha256=source.content_sha256,
        payload={
            "kind": source.kind, "origin": source.origin,
            "lines": len(source.lines), "candidates": len(candidates),
            "needs": source.needs, "notes": source.notes,
        },
    )
    return row


def ingest_file(
    session: Session,
    data: bytes,
    filename: str,
    *,
    uploaded_by: str = "system",
    rulebook: Rulebook | None = None,
    store: ObjectStore | None = None,
) -> tuple[RuleSource, list[Candidate]]:
    """Take in an uploaded file and extract candidate rules from it."""
    try:
        source = read_bytes(data, filename)
    except SourceError as exc:
        raise IntakeError(str(exc)) from exc

    # Keep the source itself, content-addressed like everything else, so a rule traced
    # back to "page 4 of the 2024 style guide" can produce that page years later.
    blob_uri = None
    if store is not None:
        digest = store.put_bytes(data, suffix=Path(filename).suffix or ".bin")
        blob_uri = store.uri(digest, suffix=Path(filename).suffix or ".bin")

    candidates = extract(source, rulebook=rulebook)
    return _persist(session, source, candidates, uploaded_by=uploaded_by,
                    blob_uri=blob_uri), candidates


def ingest_url(
    session: Session,
    url: str,
    *,
    uploaded_by: str = "system",
    rulebook: Rulebook | None = None,
) -> tuple[RuleSource, list[Candidate]]:
    """Fetch a page and extract candidate rules from it."""
    try:
        source = from_url(url)
    except SourceError as exc:
        raise IntakeError(str(exc)) from exc
    candidates = extract(source, rulebook=rulebook)
    return _persist(session, source, candidates, uploaded_by=uploaded_by), candidates


def ingest_path(
    session: Session,
    path: Path | str,
    *,
    uploaded_by: str = "system",
    rulebook: Rulebook | None = None,
    store: ObjectStore | None = None,
) -> tuple[RuleSource, list[Candidate]]:
    """Take in a file already on disk."""
    path = Path(path)
    return ingest_file(session, path.read_bytes(), path.name,
                       uploaded_by=uploaded_by, rulebook=rulebook, store=store)


# ------------------------------------------------------------------ review


def pending(session: Session, source_id: int | None = None) -> list[RuleCandidate]:
    """Candidates still awaiting a decision, best-evidenced first."""
    statement = select(RuleCandidate).where(
        RuleCandidate.status == CandidateStatus.PROPOSED
    )
    if source_id is not None:
        statement = statement.where(RuleCandidate.source_id == source_id)
    return list(session.exec(
        statement.order_by(RuleCandidate.confidence.desc(), RuleCandidate.id)  # type: ignore[union-attr]
    ).all())


def reject(
    session: Session, candidate_id: int, reviewer: str, *, note: str | None = None
) -> RuleCandidate:
    """Decline a candidate. The row stays, so the source's yield is honest."""
    candidate = session.get(RuleCandidate, candidate_id)
    if candidate is None:
        raise IntakeError(f"no candidate {candidate_id}")
    if candidate.status is not CandidateStatus.PROPOSED:
        raise IntakeError(f"candidate {candidate_id} is already {candidate.status.value}")

    candidate.status = CandidateStatus.REJECTED
    candidate.decided_by = reviewer
    candidate.decided_at = utcnow()
    candidate.decision_note = note
    session.add(candidate)
    session.flush()

    audit.record(
        session, "candidate.rejected",
        summary=f"{reviewer} rejected {candidate.deprecated!r} -> {candidate.approved!r}",
        actor=reviewer, actor_kind=ActorKind.HUMAN,
        payload={"candidate_id": candidate_id, "note": note,
                 "quote": candidate.quote, "source_id": candidate.source_id},
    )
    return candidate


def accept(
    session: Session,
    candidate_id: int,
    reviewer: str,
    *,
    rulebook_path: Path | str,
    owner: str = "",
    overrides: dict[str, Any] | None = None,
    note: str | None = None,
) -> tuple[RuleCandidate, Rulebook]:
    """Turn a candidate into a real rule and write it into the rulebook.

    ``overrides`` lets the reviewer correct the extraction before it lands - the term,
    the replacement, the match kind, scope, whether it needs judgement. A candidate
    accepted with changes is recorded as ``edited``, not ``accepted``, because "the
    machine proposed this and a person kept it" and "a person rewrote it" are different
    facts and the trail should not blur them.

    Writing the rule moves the rulebook hash, which invalidates verification of runs
    produced under the old one. That is deliberate; see :func:`pending_reruns`.
    """
    candidate = session.get(RuleCandidate, candidate_id)
    if candidate is None:
        raise IntakeError(f"no candidate {candidate_id}")
    if candidate.status is not CandidateStatus.PROPOSED:
        raise IntakeError(f"candidate {candidate_id} is already {candidate.status.value}")

    rulebook_path = Path(rulebook_path)
    book = load_rulebook(rulebook_path)
    overrides = dict(overrides or {})

    source = session.get(RuleSource, candidate.source_id)
    source_label = source.name if source else "an uploaded source"

    # `deprecated` is a single string on a Candidate and a list on a Rule, so a reviewer
    # may legitimately override it with either. A string corrects the extracted term; a
    # list is a rule-level edit (several spellings mapping to one approved term) and is
    # left for the Rule construction below rather than forced into the Candidate.
    def _string_override(field: str, fallback: str) -> str:
        value = overrides.get(field)
        if isinstance(value, str):
            overrides.pop(field)
            return value
        return fallback

    proposal = Candidate(
        deprecated=_string_override("deprecated", candidate.deprecated),
        approved=_string_override("approved", candidate.approved),
        quote=candidate.quote, locator=candidate.locator,
        source_name=source_label, method=candidate.method,
        confidence=candidate.confidence, note=candidate.note or "",
        suggested=dict(candidate.suggested),
    )

    rule_id = overrides.pop("id", None) or next_rule_id(book)
    rule = proposal.to_rule(
        rule_id,
        owner=overrides.pop("owner", owner),
        rationale=overrides.pop(
            "rationale",
            f"From {source_label} ({candidate.locator}): {candidate.quote[:160]}",
        ),
    )
    if overrides:
        # Rebuild rather than model_copy: copying bypasses validators and coercion, so a
        # scope passed as ["body", "tables"] would be stored as raw strings and only
        # surface as a serializer warning. Constructing fresh runs the full validation
        # the reviewer's edit deserves.
        try:
            rule = Rule(**{**rule.model_dump(mode="json"), **overrides})
        except Exception as exc:  # noqa: BLE001
            raise IntakeError(f"invalid rule after overrides: {exc}") from exc

    updated = Rulebook(rules=[*book.rules, rule], version=book.version)
    dump_rulebook(updated, rulebook_path)
    try:
        saved = load_rulebook(rulebook_path)   # the real validation, including overlaps
    except RuleError as exc:
        dump_rulebook(book, rulebook_path)      # put it back rather than leave it broken
        raise IntakeError(f"rule rejected by the rulebook: {exc}") from exc

    edited = bool(
        proposal.deprecated != candidate.deprecated
        or proposal.approved != candidate.approved
    )
    candidate.status = CandidateStatus.EDITED if edited else CandidateStatus.ACCEPTED
    candidate.decided_by = reviewer
    candidate.decided_at = utcnow()
    candidate.decision_note = note
    candidate.rule_id = rule.id
    session.add(candidate)
    session.flush()

    audit.record(
        session, "candidate.accepted",
        summary=(
            f"{reviewer} added {rule.id}: {rule.deprecated[0]!r} -> {rule.approved!r} "
            f"from {source_label}"
        ),
        actor=reviewer, actor_kind=ActorKind.HUMAN, rule_id=rule.id,
        payload={
            "candidate_id": candidate_id, "rule": rule.model_dump(mode="json"),
            "edited": edited, "source_id": candidate.source_id,
            "quote": candidate.quote, "locator": candidate.locator,
            "rulebook_hash": saved.hash, "previous_hash": book.hash,
            "note": note,
        },
    )
    return candidate, saved


def pending_reruns(session: Session, rulebook: Rulebook) -> list[dict[str, Any]]:
    """Runs whose rulebook hash no longer matches, and which therefore need redoing.

    Accepting a rule changes what "correct" means. Runs verified under the old rulebook
    are not wrong, but they no longer describe the current standard, and verification
    will refuse to re-verify them. Better to say so than to let someone discover it.
    """
    return [
        {
            "run_id": run.id,
            "status": run.status.value,
            "ran_under": run.rulebook_hash,
            "current": rulebook.hash,
            "started_at": run.started_at.isoformat(),
        }
        for run in session.exec(select(Run).order_by(Run.id.desc())).all()  # type: ignore[union-attr]
        if run.rulebook_hash and run.rulebook_hash != rulebook.hash
    ]


# ----------------------------------------------------------------- reading


def describe_source(session: Session, source: RuleSource) -> dict[str, Any]:
    counts = dict(session.exec(
        select(RuleCandidate.status, func.count())
        .where(RuleCandidate.source_id == source.id)
        .group_by(RuleCandidate.status)
    ).all())
    return {
        "source_id": source.id,
        "name": source.name,
        "kind": source.kind.value,
        "origin": source.origin,
        "uploaded_by": source.uploaded_by,
        "uploaded_at": source.uploaded_at.isoformat(),
        "content_sha256": source.content_sha256,
        "lines_read": source.lines_read,
        "candidates_found": source.candidates_found,
        "needs": source.needs,
        "notes": source.notes,
        "readable": source.lines_read > 0,
        "by_status": {
            (status.value if hasattr(status, "value") else str(status)): count
            for status, count in counts.items()
        },
    }


def candidate_payload(session: Session, candidate: RuleCandidate) -> dict[str, Any]:
    source = session.get(RuleSource, candidate.source_id)
    return {
        "candidate_id": candidate.id,
        "source_id": candidate.source_id,
        "source": source.name if source else "",
        "source_kind": source.kind.value if source else "",
        "deprecated": candidate.deprecated,
        "approved": candidate.approved,
        "method": candidate.method,
        "confidence": round(candidate.confidence, 2),
        "quote": candidate.quote,
        "locator": candidate.locator,
        "note": candidate.note,
        "warnings": candidate.warnings,
        "suggested": candidate.suggested,
        "status": candidate.status.value,
        "decided_by": candidate.decided_by,
        "rule_id": candidate.rule_id,
    }
