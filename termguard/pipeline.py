"""Layer D, part 0: the orchestration that runs a corpus end to end.

Sequence per document, each step recording what it did:

    ingest (v1)  ->  scan  ->  deterministic redline  ->  judge  ->  AI redline  ->  (v2)

Two details matter for correctness.

**The AI pass runs on the output of the deterministic pass**, not on the original, so the
two mechanisms compose instead of fighting over the same spans. Because the first pass
shifts the text, the judgment hits are re-derived by re-scanning the redlined bytes; their
character spans are then valid against the document the AI pass will actually edit.

**Mechanisms stay separate** (constraint 2). Deterministic changes are authored by
"TermGuard (rule engine)" and AI changes by "TermGuard (AI-proposed)", written in two
passes because ``docx-editor`` binds the revision author at open time. Every Change row
records which mechanism produced it, and the counts are reported separately everywhere.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sqlmodel import Session, select

from termguard import audit, documents, redline as redline_module
from termguard.config import Settings, get_settings
from termguard.judge import Judge, JudgeOutcome, ResponseCache
from termguard.models import (
    ActorKind,
    Change,
    ChangeStatus,
    Classification,
    Document,
    DocumentVersion,
    Hit as HitRow,
    Mechanism,
    Run,
    RunStatus,
    Stage,
)
from termguard.redline import annotate, build_comment, hit_for_llm_edit, redline
from termguard.rulebook import Rulebook, load_rulebook
from termguard.scanner import Hit, scan_bytes
from termguard.storage import ObjectStore, get_store

ProgressFn = Callable[[str, dict[str, Any]], None]

DOC_TYPE_PREFIXES = {
    "IFU": "ifu", "SOP": "sop", "RMS": "risk", "CER": "clinical",
    "LBL": "labeling", "CTL": "control",
}


@dataclass
class DocumentOutcome:
    """What the pipeline did to one document."""

    name: str
    document_id: int
    ingested_version_id: int
    redlined_version_id: int | None = None
    hits: int = 0
    deterministic: int = 0
    ai_proposed: int = 0
    ai_kept: int = 0
    ai_escalated: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return self.deterministic + self.ai_proposed


def corpus_hash(paths: Sequence[Path]) -> str:
    """Identifies the exact set of input bytes a run was performed over."""
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        digest.update(path.name.encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def _doc_type(name: str) -> str | None:
    return DOC_TYPE_PREFIXES.get(name.split("-")[0].upper())


def _persist_hits(
    session: Session, run: Run, document: Document, version: DocumentVersion,
    hits: Sequence[Hit],
) -> dict[int, HitRow]:
    """Write Hit rows and return a map from the in-memory hit's id to its row."""
    rows: dict[int, HitRow] = {}
    for hit in hits:
        row = HitRow(
            run_id=run.id,  # type: ignore[arg-type]
            document_id=document.id,  # type: ignore[arg-type]
            document_version_id=version.id,  # type: ignore[arg-type]
            rule_id=hit.rule_id,
            classification=Classification(hit.classification),
            reason=hit.reason,
            part=hit.location.part,
            location=hit.location.as_dict(),
            paragraph_index=hit.location.paragraph_index,
            is_heading=hit.location.is_heading,
            matched_text=hit.matched_text,
            approved_text=hit.approved_text,
            span_start=hit.span[0],
            span_end=hit.span[1],
            sentence=hit.sentence,
            paragraph_text=hit.paragraph_text,
            occurrence=hit.occurrence,
        )
        session.add(row)
        session.flush()
        rows[id(hit)] = row
    return rows


def _hit_key(hit: Hit) -> tuple:
    """Identity of a hit across a re-scan: location, rule and matched literal."""
    return (
        hit.location.part, hit.location.part_name, hit.location.paragraph_index,
        hit.rule_id, hit.matched_text.casefold(), hit.occurrence,
    )


def process_document(
    session: Session,
    store: ObjectStore,
    run: Run,
    path: Path,
    rulebook: Rulebook,
    judge: Judge,
    settings: Settings,
    *,
    actor: str = "system",
    progress: ProgressFn | None = None,
) -> DocumentOutcome:
    """Run one document through the whole pipeline, recording every step."""
    emit = progress or (lambda *_args, **_kwargs: None)

    # --- ingest (v1) -------------------------------------------------------
    document, ingested = documents.ingest(
        session, store, path, actor=actor, run_id=run.id, doc_type=_doc_type(path.name)
    )
    outcome = DocumentOutcome(
        name=path.name, document_id=document.id, ingested_version_id=ingested.id  # type: ignore[arg-type]
    )
    emit("document.ingested", {"file": path.name, "version": ingested.version_no})

    original = documents.content(store, ingested)

    # --- scan --------------------------------------------------------------
    hits = scan_bytes(original, path.name, rulebook)
    outcome.hits = len(hits)
    hit_rows = _persist_hits(session, run, document, ingested, hits)
    audit.record(
        session, "document.scanned",
        summary=f"{path.name}: {len(hits)} hits",
        actor=actor, run_id=run.id, document_id=document.id,
        document_version_id=ingested.id, content_sha256=ingested.content_sha256,
        payload={
            "hits": len(hits),
            "unambiguous": sum(1 for h in hits if h.classification == "unambiguous"),
            "needs_judgment": sum(1 for h in hits if h.needs_judgment),
            "by_part": {p: sum(1 for h in hits if h.part == p) for p in {h.part for h in hits}},
        },
    )
    emit("document.scanned", {"file": path.name, "hits": len(hits)})

    if not hits:
        return outcome

    # --- deterministic redline --------------------------------------------
    unambiguous = [h for h in hits if h.classification == "unambiguous"]
    comments = {id(h): build_comment(h, rulebook, "deterministic") for h in unambiguous}
    current = original
    changes: list[tuple[Hit, redline_module.AppliedChange, Mechanism, JudgeOutcome | None]] = []

    if unambiguous:
        result = redline(
            current, unambiguous, rulebook,
            author=settings.rule_engine_author, comment_texts=comments,
        )
        current = result.data
        outcome.deterministic = result.count
        outcome.skipped.extend(f"{h.rule_id} @ {h.location.describe()}: {why}"
                               for h, why in result.skipped)
        for applied in result.applied:
            changes.append((applied.hit, applied, Mechanism.DETERMINISTIC, None))
        emit("document.redlined", {"file": path.name, "changes": result.count})

    # --- judge --------------------------------------------------------------
    # Re-scan the redlined bytes so judgment hits carry spans valid against the document
    # the AI pass will edit.
    rescanned = {_hit_key(h): h for h in scan_bytes(current, path.name, rulebook)}
    judgment_hits = [
        rescanned.get(_hit_key(h), h) for h in hits if h.needs_judgment
    ]

    outcomes: list[JudgeOutcome] = judge.judge_all(judgment_hits) if judgment_hits else []

    ai_hits: list[Hit] = []
    ai_comments: dict[int, str] = {}
    ai_by_hit: dict[int, JudgeOutcome] = {}
    annotations: list[tuple[Hit, str]] = []
    annotation_by_hit: dict[int, JudgeOutcome] = {}

    for judged in outcomes:
        if judged.decision == "change":
            edit = hit_for_llm_edit(judged.hit, judged.revised_sentence)
            if edit is None:
                outcome.ai_escalated += 1
                annotations.append((
                    judged.hit,
                    build_comment(judged.hit, rulebook, "AI-proposed",
                                  extra="Escalated: the revision could not be located."),
                ))
                annotation_by_hit[id(judged.hit)] = judged
                continue
            ai_hits.append(edit)
            ai_by_hit[id(edit)] = judged
            ai_comments[id(edit)] = build_comment(
                judged.hit, rulebook, "AI-proposed",
                extra=f"{judged.justification} Model: {judged.model}. Requires reviewer decision.",
            )
        else:
            if judged.decision == "keep":
                outcome.ai_kept += 1
                extra = f"Left unchanged: {judged.justification}"
            else:
                outcome.ai_escalated += 1
                extra = f"Escalated: {judged.rejected_reason or judged.justification}"
            annotations.append((
                judged.hit,
                build_comment(judged.hit, rulebook, "AI-proposed",
                              extra=f"{extra} Model: {judged.model}."),
            ))
            annotation_by_hit[id(judged.hit)] = judged

    # --- AI redline ---------------------------------------------------------
    if ai_hits:
        ai_result = redline(
            current, ai_hits, rulebook,
            author=settings.ai_author, mechanism="AI-proposed", comment_texts=ai_comments,
        )
        current = ai_result.data
        outcome.ai_proposed = ai_result.count
        outcome.skipped.extend(f"{h.rule_id} @ {h.location.describe()}: {why}"
                               for h, why in ai_result.skipped)
        for applied in ai_result.applied:
            changes.append((applied.hit, applied, Mechanism.AI, ai_by_hit.get(id(applied.hit))))
        emit("document.judged", {"file": path.name, "ai_changes": ai_result.count})

    if annotations:
        current, annotated, unannotated = annotate(current, annotations)
        for applied in annotated:
            changes.append(
                (applied.hit, applied, Mechanism.AI, annotation_by_hit.get(id(applied.hit)))
            )
        # Comments outside the body are not written into the .docx, but the decision still
        # has to exist as a reviewable record - it is what adjudicates the remaining hit.
        comment_by_hit = {id(h): text for h, text in annotations}
        for hit, why in unannotated:
            changes.append((
                hit,
                redline_module.AppliedChange(
                    hit=hit, comment=comment_by_hit.get(id(hit), ""), engine="audit-only",
                ),
                Mechanism.AI,
                annotation_by_hit.get(id(hit)),
            ))

    # --- store the redlined version (v2) -----------------------------------
    version = documents.add_version(
        session, store, document, current, Stage.REDLINED,
        actor=f"{settings.rule_engine_author} + {settings.ai_author}",
        actor_kind=ActorKind.SYSTEM, parent=ingested, run_id=run.id,
        note="tracked changes applied, awaiting review",
        summary={
            "hits": outcome.hits,
            "deterministic": outcome.deterministic,
            "ai": outcome.ai_proposed,
            "ai_kept": outcome.ai_kept,
            "ai_escalated": outcome.ai_escalated,
        },
    )
    outcome.redlined_version_id = version.id

    # --- persist Change rows ------------------------------------------------
    for hit, applied, mechanism, judged in changes:
        row = hit_rows.get(id(hit))
        if row is None:
            # AI edits are re-derived hits; match them back by location and rule.
            row = next(
                (r for r in hit_rows.values()
                 if r.rule_id == hit.rule_id
                 and r.part == hit.location.part
                 and r.paragraph_index == hit.location.paragraph_index
                 and r.classification == Classification.NEEDS_JUDGMENT),
                None,
            )
        change = Change(
            run_id=run.id,  # type: ignore[arg-type]
            hit_id=row.id if row else None,  # type: ignore[arg-type]
            document_id=document.id,  # type: ignore[arg-type]
            document_version_id=version.id,
            mechanism=mechanism,
            status=ChangeStatus.PROPOSED if applied.revision_id is not None
            else ChangeStatus.COMMENTED,
            original_text=hit.matched_text,
            proposed_text=hit.approved_text if applied.revision_id is not None else "",
            comment=applied.comment,
            revision_id=applied.revision_id,
            revision_ids=list(applied.revision_ids),
            comment_id=applied.comment_id,
            applied_at=version.created_at,
            model=judged.model if judged else None,
            prompt_version=judged.prompt_version if judged else None,
            prompt_hash=judged.prompt_hash if judged else None,
            request_id=judged.request_id if judged else None,
            latency_ms=judged.latency_ms if judged else None,
            justification=judged.justification if judged else None,
            llm_decision=judged.decision if judged else None,
        )
        session.add(change)
        session.flush()

        audit.record(
            session, "change.proposed",
            summary=f"{hit.rule_id} {hit.matched_text!r} -> {hit.approved_text!r} "
                    f"at {hit.location.describe()}",
            actor=settings.ai_author if mechanism is Mechanism.AI else settings.rule_engine_author,
            actor_kind=ActorKind.LLM if mechanism is Mechanism.AI else ActorKind.RULE_ENGINE,
            run_id=run.id, document_id=document.id, document_version_id=version.id,
            hit_id=row.id if row else None, change_id=change.id,
            mechanism=mechanism, rule_id=hit.rule_id,
            content_sha256=version.content_sha256,
            payload={
                "engine": applied.engine,
                "part": hit.location.part,
                "location": hit.location.container_path,
                "original": hit.matched_text,
                "proposed": hit.approved_text,
                **({"model": judged.model, "prompt_hash": judged.prompt_hash,
                    "llm_decision": judged.decision,
                    "request_id": judged.request_id} if judged else {}),
            },
        )

    # --- write the reviewable copy -----------------------------------------
    settings.redlined_dir.mkdir(parents=True, exist_ok=True)
    (settings.redlined_dir / f"{path.stem}.redlined.docx").write_bytes(current)

    return outcome


def run_pipeline(
    session: Session,
    *,
    settings: Settings | None = None,
    store: ObjectStore | None = None,
    rulebook: Rulebook | None = None,
    corpus_dir: Path | None = None,
    dry_run: bool = True,
    actor: str = "system",
    progress: ProgressFn | None = None,
    note: str | None = None,
) -> tuple[Run, list[DocumentOutcome]]:
    """Run the whole pipeline over a corpus. Returns the Run row and per-document results."""
    settings = settings or get_settings()
    store = store or get_store(settings)
    rulebook = rulebook or load_rulebook(settings.rulebook_path)
    corpus_dir = Path(corpus_dir or settings.corpus_dir)
    emit = progress or (lambda *_args, **_kwargs: None)

    paths = sorted(corpus_dir.glob("*.docx"))
    run = Run(
        rulebook_hash=rulebook.hash,
        corpus_hash=corpus_hash(paths),
        corpus_dir=str(corpus_dir),
        dry_run=dry_run,
        actor=actor,
        note=note,
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()

    audit.record(
        session, "run.started",
        summary=f"run {run.id} over {len(paths)} files with rulebook {rulebook.hash}",
        actor=actor, run_id=run.id,
        payload={"files": len(paths), "rulebook_hash": rulebook.hash,
                 "corpus_hash": run.corpus_hash, "dry_run": dry_run,
                 "rules": len(rulebook)},
    )
    emit("run.started", {"run_id": run.id, "files": len(paths)})

    judge = Judge(
        rulebook, settings,
        live=not dry_run and settings.llm_live,
        cache=ResponseCache(settings.out_dir / "judge-cache.db"),
    )

    outcomes: list[DocumentOutcome] = []
    try:
        for index, path in enumerate(paths, start=1):
            emit("document.started", {"file": path.name, "index": index, "of": len(paths)})
            outcomes.append(
                process_document(
                    session, store, run, path, rulebook, judge, settings,
                    actor=actor, progress=progress,
                )
            )
            session.commit()

        run.status = RunStatus.COMPLETE
        run.stats = {
            "files": len(paths),
            "hits": sum(o.hits for o in outcomes),
            "deterministic": sum(o.deterministic for o in outcomes),
            "ai": sum(o.ai_proposed for o in outcomes),
            "ai_kept": sum(o.ai_kept for o in outcomes),
            "ai_escalated": sum(o.ai_escalated for o in outcomes),
            "skipped": sum(len(o.skipped) for o in outcomes),
            "judge": judge.stats,
        }
    except Exception as exc:  # noqa: BLE001
        run.status = RunStatus.FAILED
        run.note = f"{type(exc).__name__}: {exc}"
        audit.record(session, "run.failed", summary=str(exc), actor=actor, run_id=run.id)
        session.add(run)
        session.commit()
        raise

    from termguard.models import utcnow

    run.finished_at = utcnow()
    session.add(run)
    audit.record(
        session, "run.completed",
        summary=f"run {run.id}: {run.stats['deterministic']} deterministic, "
                f"{run.stats['ai']} AI-proposed across {len(paths)} files",
        actor=actor, run_id=run.id, payload=run.stats,
    )
    session.commit()
    emit("run.completed", run.stats)
    return run, outcomes
