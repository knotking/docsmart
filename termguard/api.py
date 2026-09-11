"""Layer D: the HTTP surface.

The UI is a view over this; nothing in the dashboard computes anything the API does not
already expose, so every number on screen can be traced to a row.

Endpoint groups:

* **runs** - start a pipeline run, stream its progress, read its summary and hits
* **review** - the queue and the decision endpoint (append-only)
* **verification** - run the gate, download the report
* **audit** - the exportable CSV
* **documents** - the lifecycle: version chain, timeline, point-in-time download,
  and an integrity check. This is what answers "what did this file look like then, and
  what happened to it".
* **rulebook** - read and replace, with validation and a hash bump

Deliberately absent: authentication and multi-tenancy (see CLAUDE.md, "what not to build").
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Iterator

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from termguard import __version__, audit, documents, review, verify
from termguard.config import get_settings
from termguard.db import get_session, init_db
from termguard.models import (
    Change,
    ChangeStatus,
    Decision,
    DecisionKind,
    Document,
    DocumentVersion,
    Hit,
    Mechanism,
    Run,
    RunStatus,
    Stage,
)
from termguard.pipeline import run_pipeline
from termguard.rulebook import Rule, RuleError, Rulebook, dump_rulebook, load_rulebook
from termguard.storage import get_store

app = FastAPI(
    title="TermGuard",
    version=__version__,
    description="Terminology harmonization for regulated .docx document sets.",
)

# The dashboard is served by Vite in development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Progress queues for in-flight runs, consumed by the SSE endpoint.
_PROGRESS: dict[int, Queue] = {}
_ACTIVE: dict[str, int] = {}


@app.on_event("startup")
def _startup() -> None:
    init_db(get_settings())


# ------------------------------------------------------------------ schemas


class DecisionRequest(BaseModel):
    decision: str = Field(description="accepted | rejected | edited")
    reviewer: str
    final_text: str | None = None
    note: str | None = None


class RunRequest(BaseModel):
    corpus_dir: str | None = None
    dry_run: bool = True
    actor: str = "api"
    note: str | None = None


class RulebookRequest(BaseModel):
    rules: list[dict[str, Any]]
    version: str = "1"


# ------------------------------------------------------------------- health


@app.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "storage": settings.storage_backend,
        "database": settings.db_url.split("://")[0],
        "model": settings.anthropic_model,
        "llm_live": settings.llm_live,
    }


# --------------------------------------------------------------------- runs


def _run_in_background(run_request: RunRequest, queue: Queue, token: str) -> None:
    """Execute a pipeline run on its own thread, publishing progress to ``queue``."""
    from termguard.db import session_scope

    settings = get_settings()
    try:
        with session_scope(settings) as session:
            def progress(event: str, payload: dict[str, Any]) -> None:
                if event == "run.started":
                    _ACTIVE[token] = payload["run_id"]
                queue.put({"event": event, "data": payload})

            run, _ = run_pipeline(
                session,
                settings=settings,
                corpus_dir=Path(run_request.corpus_dir) if run_request.corpus_dir else None,
                dry_run=run_request.dry_run,
                actor=run_request.actor,
                note=run_request.note,
                progress=progress,
            )
            queue.put({"event": "done", "data": {"run_id": run.id, "status": run.status.value}})
    except Exception as exc:  # noqa: BLE001
        queue.put({"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}})
    finally:
        queue.put(None)  # sentinel: stream complete


@app.post("/runs", status_code=202)
def start_run(request: RunRequest = Body(default=RunRequest())) -> dict[str, Any]:
    """Start a pipeline run in the background. Poll ``/runs/{id}`` or stream its events."""
    token = f"run-{len(_PROGRESS) + 1}"
    queue: Queue = Queue()
    _PROGRESS[token] = queue  # type: ignore[index]
    Thread(target=_run_in_background, args=(request, queue, token), daemon=True).start()
    return {"token": token, "events": f"/runs/stream/{token}"}


@app.get("/runs/stream/{token}")
def stream_run(token: str) -> StreamingResponse:
    """Server-sent events for an in-flight run."""
    queue = _PROGRESS.get(token)
    if queue is None:
        raise HTTPException(404, f"no run stream {token}")

    def events() -> Iterator[str]:
        while True:
            try:
                item = queue.get(timeout=120)
            except Empty:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                break
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/runs")
def list_runs(session: Session = Depends(get_session), limit: int = 20) -> list[dict[str, Any]]:
    runs = session.exec(select(Run).order_by(Run.id.desc()).limit(limit)).all()  # type: ignore[union-attr]
    return [_run_summary(session, run, shallow=True) for run in runs]


@app.get("/runs/latest")
def latest_run(session: Session = Depends(get_session)) -> dict[str, Any]:
    """The most recent run, so the dashboard is never empty on load."""
    run = session.exec(select(Run).order_by(Run.id.desc())).first()  # type: ignore[union-attr]
    if run is None:
        raise HTTPException(404, "no runs yet")
    return _run_summary(session, run)


@app.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"no run {run_id}")
    return _run_summary(session, run)


def _count(session: Session, model: Any, *conditions: Any) -> int:
    statement = select(func.count()).select_from(model)
    for condition in conditions:
        statement = statement.where(condition)
    return session.exec(statement).one()


def _group(session: Session, column: Any, *conditions: Any) -> dict[str, int]:
    statement = select(column, func.count()).group_by(column)
    for condition in conditions:
        statement = statement.where(condition)
    out: dict[str, int] = {}
    for key, count in session.exec(statement).all():
        out[key.value if hasattr(key, "value") else str(key)] = count
    return out


def _run_summary(session: Session, run: Run, *, shallow: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "run_id": run.id,
        "status": run.status.value,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "rulebook_hash": run.rulebook_hash,
        "corpus_hash": run.corpus_hash,
        "corpus_dir": run.corpus_dir,
        "dry_run": run.dry_run,
        "actor": run.actor,
        "note": run.note,
        "stats": run.stats,
    }
    if shallow:
        return summary

    decided = {
        row.change_id for row in session.exec(
            select(Decision).where(Decision.run_id == run.id)
        ).all()
    }
    total_changes = _count(session, Change, Change.run_id == run.id)

    summary.update(
        {
            "hits": {
                "total": _count(session, Hit, Hit.run_id == run.id),
                "by_classification": _group(session, Hit.classification, Hit.run_id == run.id),
                "by_part": _group(session, Hit.part, Hit.run_id == run.id),
                "by_rule": _group(session, Hit.rule_id, Hit.run_id == run.id),
            },
            "changes": {
                "total": total_changes,
                "by_mechanism": _group(session, Change.mechanism, Change.run_id == run.id),
                "decided": len(decided),
                "pending": total_changes - len(decided),
            },
            "decisions": _group(session, Decision.decision, Decision.run_id == run.id),
            "files": _files_for_run(session, run.id),
        }
    )
    return summary


def _files_for_run(session: Session, run_id: int) -> list[dict[str, Any]]:
    rows = session.exec(
        select(Document, Hit.classification, func.count())
        .join(Hit, Hit.document_id == Document.id)  # type: ignore[arg-type]
        .where(Hit.run_id == run_id)
        .group_by(Document.id, Hit.classification)
    ).all()

    files: dict[int, dict[str, Any]] = {}
    for document, classification, count in rows:
        entry = files.setdefault(
            document.id,
            {"document_id": document.id, "name": document.name, "doc_type": document.doc_type,
             "hits": 0, "unambiguous": 0, "needs_judgment": 0},
        )
        entry["hits"] += count
        entry[classification.value] += count

    # Files scanned in this run that produced no hits still belong in the list.
    scanned = session.exec(
        select(Document)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)  # type: ignore[arg-type]
        .where(DocumentVersion.run_id == run_id)
        .distinct()
    ).all()
    for document in scanned:
        files.setdefault(
            document.id,
            {"document_id": document.id, "name": document.name, "doc_type": document.doc_type,
             "hits": 0, "unambiguous": 0, "needs_judgment": 0},
        )

    for entry in files.values():
        entry["mechanisms"] = _group(
            session, Change.mechanism,
            Change.run_id == run_id, Change.document_id == entry["document_id"],
        )
    return sorted(files.values(), key=lambda f: f["name"])


@app.get("/runs/{run_id}/hits")
def list_hits(
    run_id: int,
    session: Session = Depends(get_session),
    file: str | None = None,
    rule: str | None = None,
    classification: str | None = None,
    part: str | None = None,
    limit: int = Query(default=500, le=5000),
    offset: int = 0,
) -> dict[str, Any]:
    statement = select(Hit).where(Hit.run_id == run_id)
    if rule:
        statement = statement.where(Hit.rule_id == rule)
    if classification:
        statement = statement.where(Hit.classification == classification)
    if part:
        statement = statement.where(Hit.part == part)
    if file:
        document = session.exec(select(Document).where(Document.name == file)).first()
        if document is None:
            return {"total": 0, "hits": []}
        statement = statement.where(Hit.document_id == document.id)

    rows = session.exec(statement.order_by(Hit.id).offset(offset).limit(limit)).all()
    names = {d.id: d.name for d in session.exec(select(Document)).all()}
    return {
        "total": len(rows),
        "hits": [
            {
                "hit_id": hit.id,
                "file": names.get(hit.document_id, ""),
                "rule_id": hit.rule_id,
                "classification": hit.classification.value,
                "reason": hit.reason,
                "part": hit.part,
                "location": hit.location.get("container_path"),
                "paragraph_index": hit.paragraph_index,
                "is_heading": hit.is_heading,
                "matched_text": hit.matched_text,
                "approved_text": hit.approved_text,
                "sentence": hit.sentence,
            }
            for hit in rows
        ],
    }


# ------------------------------------------------------------------- review


@app.get("/runs/{run_id}/queue")
def review_queue(
    run_id: int,
    session: Session = Depends(get_session),
    file: str | None = None,
    rule: str | None = None,
    mechanism: str | None = None,
    status: str = Query(default="pending", description="pending | decided | all"),
    limit: int = Query(default=500, le=5000),
) -> dict[str, Any]:
    """The reviewer queue: everything needed to decide, in one payload per item."""
    decided = {
        row.change_id for row in session.exec(
            select(Decision).where(Decision.run_id == run_id)
        ).all()
    }
    statement = select(Change).where(Change.run_id == run_id)
    if mechanism:
        statement = statement.where(Change.mechanism == mechanism)
    if file:
        document = session.exec(select(Document).where(Document.name == file)).first()
        if document is None:
            return {"total": 0, "items": []}
        statement = statement.where(Change.document_id == document.id)

    changes = session.exec(statement.order_by(Change.id)).all()
    if status == "pending":
        changes = [c for c in changes if c.id not in decided]
    elif status == "decided":
        changes = [c for c in changes if c.id in decided]

    items = [review.queue_item(session, change) for change in changes]
    if rule:
        items = [i for i in items if i["rule_id"] == rule]
    return {"total": len(items), "items": items[:limit]}


@app.post("/changes/{change_id}/decision")
def decide(
    change_id: int,
    request: DecisionRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Record a reviewer decision. Append-only: a new ruling supersedes, never overwrites."""
    try:
        decision = review.record_decision(
            session, change_id, request.decision, request.reviewer,
            final_text=request.final_text, note=request.note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {
        "decision_id": decision.id,
        "change_id": change_id,
        "decision": decision.decision.value,
        "reviewer": decision.reviewer,
        "decided_at": decision.decided_at.isoformat(),
    }


@app.get("/changes/{change_id}/decisions")
def decision_history(change_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Every ruling ever made on a change, oldest first. Superseded rows remain."""
    rows = session.exec(
        select(Decision).where(Decision.change_id == change_id).order_by(Decision.id)
    ).all()
    return [
        {"decision_id": r.id, "decision": r.decision.value, "reviewer": r.reviewer,
         "final_text": r.final_text, "note": r.note, "decided_at": r.decided_at.isoformat(),
         "superseded": index < len(rows) - 1}
        for index, r in enumerate(rows)
    ]


# ------------------------------------------------------------- verification


@app.post("/runs/{run_id}/verify")
def run_verification(
    run_id: int,
    session: Session = Depends(get_session),
    actor: str = "api",
) -> dict[str, Any]:
    """Build the as-accepted corpus, re-scan it and return the verdict."""
    try:
        report = verify.verify_run(session, run_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return report.to_dict()


@app.get("/runs/{run_id}/verification.md", response_class=PlainTextResponse)
def verification_markdown(run_id: int, session: Session = Depends(get_session)) -> str:
    settings = get_settings()
    path = settings.out_dir / "verification.md"
    if not path.exists():
        raise HTTPException(404, "no verification report yet; POST /runs/{id}/verify first")
    return path.read_text()


# ------------------------------------------------------------------- audit


@app.get("/runs/{run_id}/audit.csv")
def audit_csv(run_id: int, session: Session = Depends(get_session)) -> Response:
    """One row per change, with provenance and the reviewer's ruling."""
    return Response(
        content=audit.audit_csv(session, run_id),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="termguard-run-{run_id}-audit.csv"'},
    )


@app.get("/runs/{run_id}/events")
def run_events(
    run_id: int, session: Session = Depends(get_session), limit: int = Query(500, le=5000)
) -> list[dict[str, Any]]:
    """The append-only event log for a run."""
    return [
        {"id": e.id, "ts": e.ts.isoformat(), "event": e.event, "summary": e.summary,
         "actor": e.actor, "actor_kind": e.actor_kind.value,
         "mechanism": e.mechanism.value if e.mechanism else None,
         "rule_id": e.rule_id, "payload": e.payload}
        for e in audit.events_for_run(session, run_id)[:limit]
    ]


# --------------------------------------------------------------- documents
#
# The lifecycle surface: what a document looked like at every point, what changed it,
# and proof the archive has not rotted.


@app.get("/documents")
def list_documents(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for document in session.exec(select(Document).order_by(Document.name)).all():
        versions = documents.history(session, document.id)  # type: ignore[arg-type]
        latest = versions[-1] if versions else None
        out.append(
            {
                "document_id": document.id,
                "name": document.name,
                "doc_type": document.doc_type,
                "created_at": document.created_at.isoformat(),
                "versions": len(versions),
                "current_stage": latest.stage.value if latest else None,
                "current_version": latest.version_no if latest else None,
                "current_sha256": latest.content_sha256 if latest else None,
            }
        )
    return out


@app.get("/documents/{document_id}/versions")
def document_versions(document_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """The immutable version chain, oldest first."""
    versions = documents.history(session, document_id)
    if not versions:
        raise HTTPException(404, f"no document {document_id}")
    return [
        {
            "version_id": v.id,
            "version_no": v.version_no,
            "stage": v.stage.value,
            "sha256": v.content_sha256,
            "blob_uri": v.blob_uri,
            "size_bytes": v.size_bytes,
            "parent_version_id": v.parent_version_id,
            "run_id": v.run_id,
            "actor": v.actor,
            "actor_kind": v.actor_kind.value,
            "created_at": v.created_at.isoformat(),
            "note": v.note,
            "summary": v.summary,
            "changes": len(documents.changes_for_version(session, v.id)),  # type: ignore[arg-type]
        }
        for v in versions
    ]


@app.get("/documents/{document_id}/timeline")
def document_timeline(document_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Versions and events interleaved - one document's whole life, in order."""
    timeline = documents.document_timeline(session, document_id)
    if not timeline:
        raise HTTPException(404, f"no document {document_id}")
    return timeline


@app.get("/documents/{document_id}/versions/{version_no}/download")
def download_version(
    document_id: int, version_no: int, session: Session = Depends(get_session)
) -> Response:
    """The exact bytes of any past version. The point of keeping the chain."""
    version = next(
        (v for v in documents.history(session, document_id) if v.version_no == version_no), None
    )
    if version is None:
        raise HTTPException(404, f"no version {version_no} of document {document_id}")
    document = session.get(Document, document_id)
    store = get_store()
    name = f"{Path(document.name).stem}.v{version_no}.{version.stage.value}.docx"
    return Response(
        content=documents.content(store, version),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/runs/{run_id}/files/{name}/redlined")
def download_redlined(run_id: int, name: str, session: Session = Depends(get_session)) -> Response:
    """The reviewable copy: the version with tracked changes in it."""
    return _download_stage(session, name, Stage.REDLINED)


@app.get("/runs/{run_id}/files/{name}/final")
def download_final(run_id: int, name: str, session: Session = Depends(get_session)) -> Response:
    """The as-accepted copy produced by the verification gate."""
    for stage in (Stage.VERIFIED, Stage.FINAL):
        try:
            return _download_stage(session, name, stage)
        except HTTPException:
            continue
    raise HTTPException(404, f"no final version of {name}; run verification first")


def _download_stage(session: Session, name: str, stage: Stage) -> Response:
    document = session.exec(select(Document).where(Document.name == name)).first()
    if document is None:
        raise HTTPException(404, f"no document {name}")
    version = documents.latest_at_stage(session, document.id, stage)  # type: ignore[arg-type]
    if version is None:
        raise HTTPException(404, f"{name} has no {stage.value} version")
    filename = f"{Path(name).stem}.{stage.value}.docx"
    return Response(
        content=documents.content(get_store(), version),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/integrity")
def integrity(session: Session = Depends(get_session), document_id: int | None = None) -> dict[str, Any]:
    """Re-hash every stored version and confirm it matches its recorded digest."""
    return documents.verify_integrity(session, get_store(), document_id)


# -------------------------------------------------------------------- rulebook


@app.get("/rulebook")
def read_rulebook() -> dict[str, Any]:
    settings = get_settings()
    try:
        rulebook = load_rulebook(settings.rulebook_path)
    except RuleError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {
        "hash": rulebook.hash,
        "version": rulebook.version,
        "path": str(settings.rulebook_path),
        "modified_at": _mtime(settings.rulebook_path),
        "rules": [r.model_dump(mode="json") for r in rulebook.rules],
    }


@app.put("/rulebook")
def replace_rulebook(request: RulebookRequest) -> dict[str, Any]:
    """Validate and write a new rulebook. A successful write bumps the hash."""
    settings = get_settings()
    try:
        rules = [Rule(**entry) for entry in request.rules]
    except Exception as exc:  # noqa: BLE001 - pydantic raises a variety here
        raise HTTPException(400, f"invalid rule: {exc}") from exc

    candidate = Rulebook(rules=rules, version=request.version)
    previous = load_rulebook(settings.rulebook_path).hash
    dump_rulebook(candidate, settings.rulebook_path)

    try:
        saved = load_rulebook(settings.rulebook_path)  # re-load so validation is the real one
    except RuleError as exc:
        raise HTTPException(400, f"rulebook rejected: {exc}") from exc

    return {
        "hash": saved.hash,
        "previous_hash": previous,
        "changed": saved.hash != previous,
        "rules": len(saved),
        "modified_at": _mtime(settings.rulebook_path),
    }


def _mtime(path: Path) -> str | None:
    from datetime import datetime, timezone

    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
