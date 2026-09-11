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
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Iterator

from fastapi import (
    BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Query,
    Response, UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from termguard import __version__, audit, documents, metrics, review, verify, workflow
from termguard import intake
from termguard import teams as teams_module
from termguard.config import get_settings
from termguard.db import get_session, init_db
from termguard.models import (
    Change,
    Organization,
    Participant,
    ChangeStatus,
    Decision,
    DecisionKind,
    Document,
    DocumentVersion,
    Hit,
    Mechanism,
    Run,
    RuleCandidate,
    RuleSource,
    RunStatus,
    Stage,
    Team,
)
from termguard.pipeline import run_pipeline
from termguard.policy import load_policy
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
    enforce_claim: bool = True


class RunRequest(BaseModel):
    corpus_dir: str | None = None
    dry_run: bool = True
    actor: str = "api"
    note: str | None = None


class RulebookRequest(BaseModel):
    rules: list[dict[str, Any]]
    version: str = "1"


class ParticipantRequest(BaseModel):
    name: str
    kind: str = Field(default="human", description="human | agent")
    roles: list[str] = Field(default_factory=lambda: ["reviewer"])
    email: str | None = None
    model: str | None = None
    note: str | None = None


class AssignRequest(BaseModel):
    participants: list[str]
    strategy: str = Field(default="by_file", description="by_file | by_rule | round_robin")
    assigned_by: str = "api"


class ClaimRequest(BaseModel):
    participant: str
    lease_seconds: int = 600


class AgentDisposeRequest(BaseModel):
    agent: str = "termguard-agent"
    dry_run: bool = False


class ConfirmRequest(BaseModel):
    participant: str
    clause_id: str | None = None


class SourceUrlRequest(BaseModel):
    url: str
    uploaded_by: str = "api"


class AcceptCandidateRequest(BaseModel):
    reviewer: str
    owner: str = ""
    note: str | None = None
    overrides: dict[str, Any] | None = Field(
        default=None,
        description="corrections to apply before the rule lands: deprecated, approved, "
                    "match, case, scope, context_required, context_note, rationale",
    )


class RejectCandidateRequest(BaseModel):
    reviewer: str
    note: str | None = None


class MemberRequest(BaseModel):
    participant: str
    role: str = Field(default="member", description="member | lead")


class DelegateRequest(BaseModel):
    to: str
    until: str | None = Field(default=None, description="ISO-8601 datetime")
    reason: str | None = None
    created_by: str = "api"


class HandoffRequest(BaseModel):
    actor: str
    reason: str
    to_participant: str | None = None
    to_team: str | None = None


class SignOffRequest(BaseModel):
    participant: str
    decision: str = Field(description="approved | rejected")
    note: str | None = None
    force: bool = False


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
    # A known participant is attributed properly; an unknown name still works, so a
    # single-reviewer demo never has to register anybody first.
    participant = None
    try:
        participant = workflow.get_participant(session, request.reviewer)
    except workflow.WorkflowError:
        participant = None

    try:
        decision = review.record_decision(
            session, change_id, request.decision, request.reviewer,
            final_text=request.final_text, note=request.note,
            participant=participant, enforce_claim=request.enforce_claim,
        )
    except workflow.ClaimConflict as exc:
        raise HTTPException(409, str(exc)) from exc
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


# -------------------------------------------------------------------- metrics


@app.get("/metrics")
def metrics_dashboard(
    session: Session = Depends(get_session), run_id: int | None = None
) -> dict[str, Any]:
    """Everything the metrics screen needs, in one round trip."""
    return metrics.dashboard(session, run_id)


@app.get("/metrics/rules")
def metrics_rules(
    session: Session = Depends(get_session), run_id: int | None = None
) -> list[dict[str, Any]]:
    """Per-rule volume and override rate - what the terminology owner acts on."""
    return metrics.rule_health(session, run_id)


# --------------------------------------------------------------------- policy


@app.get("/policy")
def read_policy() -> dict[str, Any]:
    """The agent-authority policy: what a machine is permitted to decide, and why.

    ``present`` distinguishes "no policy file" from "a policy that deliberately grants
    nothing". Both delegate zero authority, which is the safe default either way, but
    only one of them is a configuration mistake.
    """
    path = _policy_path()
    loaded = load_policy(path)
    return {
        **loaded.summary(),
        "present": path.exists(),
        "path": str(path),
        "default_risk": loaded.default_risk.value,
        "risk_by_rule": {k: v.value for k, v in loaded.risk_by_rule.items()},
        "clauses": [c.model_dump(mode="json") for c in loaded.clauses],
    }


def _policy_path() -> Path:
    settings = get_settings()
    return Path(os.environ.get("TERMGUARD_POLICY", settings.rulebook_path.parent / "policy.yaml"))


# --------------------------------------------------------------- participants


@app.get("/participants")
def list_participants(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = session.exec(select(Participant).order_by(Participant.name)).all()
    return [
        {
            "participant_id": p.id,
            "name": p.name,
            "email": p.email,
            "kind": p.kind.value,
            "roles": p.roles,
            "active": p.active,
            "model": p.model,
            "policy_hash": p.policy_hash,
            "decisions": session.exec(
                select(func.count()).select_from(Decision).where(Decision.reviewer == p.name)
            ).one(),
        }
        for p in rows
    ]


@app.post("/participants", status_code=201)
def create_participant(
    request: ParticipantRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        participant = workflow.ensure_participant(
            session, request.name, kind=request.kind, roles=request.roles,
            email=request.email, model=request.model, note=request.note,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {"participant_id": participant.id, "name": participant.name,
            "kind": participant.kind.value, "roles": participant.roles}


# -------------------------------------------------------------- assignment


@app.post("/runs/{run_id}/assign")
def assign_changes(
    run_id: int, request: AssignRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Spread the undecided changes across reviewers."""
    try:
        participants = [workflow.get_participant(session, name) for name in request.participants]
        spread = workflow.auto_assign(
            session, run_id, participants, strategy=request.strategy,
            assigned_by=request.assigned_by,
        )
    except workflow.WorkflowError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {"strategy": request.strategy, "assigned": spread}


# ------------------------------------------------------------------ claims


@app.post("/changes/{change_id}/claim")
def claim_change(
    change_id: int, request: ClaimRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Take the lease on a change so another reviewer cannot decide it underneath you."""
    try:
        participant = workflow.get_participant(session, request.participant)
        held = workflow.claim(session, change_id, participant,
                              lease_seconds=request.lease_seconds)
    except workflow.ClaimConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except workflow.WorkflowError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {"change_id": change_id, "participant": request.participant,
            "expires_at": held.expires_at.isoformat()}


@app.delete("/changes/{change_id}/claim")
def release_change(
    change_id: int, participant: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        who = workflow.get_participant(session, participant)
    except workflow.WorkflowError as exc:
        raise HTTPException(400, str(exc)) from exc
    released = workflow.release(session, change_id, who)
    session.commit()
    return {"change_id": change_id, "released": released}


# ------------------------------------------------------------ agent actions


@app.post("/runs/{run_id}/agent-dispose")
def agent_dispose(
    run_id: int, request: AgentDisposeRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Let an agent decide everything the policy authorizes; leave the rest to humans."""
    loaded = load_policy(_policy_path())
    try:
        agent = workflow.ensure_participant(
            session, request.agent, kind="agent", roles=["reviewer"],
            model=get_settings().anthropic_model, policy_hash=loaded.hash,
        )
        result = workflow.agent_dispose(session, run_id, agent, loaded, dry_run=request.dry_run)
    except workflow.WorkflowError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {
        "agent": result.agent, "policy_hash": result.policy_hash,
        "decided": result.decided, "left_to_humans": result.left_to_humans,
        "needs_confirmation": result.needs_confirmation, "by_clause": result.by_clause,
        "dry_run": request.dry_run,
    }


@app.post("/runs/{run_id}/confirm-agent-batch")
def confirm_agent_batch(
    run_id: int, request: ConfirmRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """A human confirming agent decisions a clause said needed confirming."""
    try:
        confirmer = workflow.get_participant(session, request.participant)
        count = workflow.confirm_agent_batch(session, run_id, confirmer, request.clause_id)
    except workflow.WorkflowError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {"confirmed": count, "by": request.participant, "clause_id": request.clause_id}


# ----------------------------------------------------------------- sign-off


@app.get("/runs/{run_id}/signoff")
def signoff_readiness(run_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """What is blocking sign-off, and who is eligible to give it."""
    return workflow.signoff_readiness(session, run_id)


@app.post("/runs/{run_id}/signoff")
def sign_off_run(
    run_id: int, request: SignOffRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Approve or reject a whole run. Separation of duties has no override."""
    try:
        participant = workflow.get_participant(session, request.participant)
        row = workflow.sign_off(
            session, run_id, participant, request.decision,
            note=request.note, policy=load_policy(_policy_path()), force=request.force,
        )
    except workflow.WorkflowError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "signoff_id": row.id, "run_id": run_id, "participant": participant.name,
        "decision": row.decision.value, "signed_at": row.signed_at.isoformat(),
        "covered": row.covered, "rulebook_hash": row.rulebook_hash,
        "policy_hash": row.policy_hash,
    }


# ------------------------------------------------------------ org and teams


@app.get("/orgs")
def list_orgs(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = session.exec(select(Organization).order_by(Organization.name)).all()
    return [
        {"org_id": o.id, "name": o.name, "slug": o.slug,
         "teams": len(session.exec(select(Team).where(Team.org_id == o.id)).all())}
        for o in rows
    ]


@app.get("/teams")
def list_teams(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Teams with their members, leads, and the rules they own."""
    rulebook = load_rulebook(get_settings().rulebook_path)
    owned: dict[str, list[str]] = {}
    for rule in rulebook:
        if rule.owner:
            owned.setdefault(rule.owner, []).append(rule.id)

    out: list[dict[str, Any]] = []
    for team in session.exec(select(Team).order_by(Team.slug)).all():
        team_members = teams_module.members(session, team)
        lead_names = {p.name for p in teams_module.leads(session, team)}
        out.append(
            {
                "team_id": team.id,
                "slug": team.slug,
                "name": team.name,
                "description": team.description,
                "owns_rules": sorted(owned.get(team.slug, [])),
                "members": [
                    {"name": p.name, "kind": p.kind.value, "roles": p.roles,
                     "lead": p.name in lead_names}
                    for p in team_members
                ],
            }
        )
    return out


@app.post("/teams/from-rulebook", status_code=201)
def seed_teams(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Create one team per distinct rule owner in the rulebook.

    The rulebook already records who owns each rule, so the team list is derived from it
    rather than maintained alongside it and allowed to drift.
    """
    rulebook = load_rulebook(get_settings().rulebook_path)
    org = teams_module.default_org(session)
    created = teams_module.teams_from_rulebook(session, rulebook, org)
    session.commit()
    return {
        "org": org.slug,
        "teams": sorted(created),
        "owners_without_a_team": teams_module.unrouted_owners(session, rulebook),
    }


@app.post("/teams/{slug}/members", status_code=201)
def add_team_member(
    slug: str, request: MemberRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        team = teams_module.get_team(session, slug)
        participant = workflow.get_participant(session, request.participant)
        teams_module.add_member(session, team, participant, role=request.role)
    except (teams_module.TeamError, workflow.WorkflowError) as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {"team": slug, "participant": request.participant, "role": request.role}


@app.delete("/teams/{slug}/members/{name}")
def remove_team_member(
    slug: str, name: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        team = teams_module.get_team(session, slug)
        participant = workflow.get_participant(session, name)
    except (teams_module.TeamError, workflow.WorkflowError) as exc:
        raise HTTPException(400, str(exc)) from exc
    removed = teams_module.remove_member(session, team, participant)
    session.commit()
    return {"team": slug, "participant": name, "removed": removed}


# ------------------------------------------------------------- delegation


@app.get("/participants/{name}")
def describe_participant(name: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """A participant with their teams and any cover in force."""
    try:
        participant = workflow.get_participant(session, name)
    except workflow.WorkflowError as exc:
        raise HTTPException(404, str(exc)) from exc
    return teams_module.describe(session, participant)


@app.post("/participants/{name}/delegate")
def create_delegation(
    name: str, request: DelegateRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Hand this participant's queue to someone else while they are away."""
    from datetime import datetime

    try:
        participant = workflow.get_participant(session, name)
        cover = workflow.get_participant(session, request.to)
        until = datetime.fromisoformat(request.until) if request.until else None
        row = teams_module.delegate(
            session, participant, cover, until=until, reason=request.reason,
            created_by=request.created_by,
        )
    except (teams_module.TeamError, workflow.WorkflowError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {"participant": name, "covered_by": request.to,
            "until": row.ends_at.isoformat() if row.ends_at else None}


@app.delete("/participants/{name}/delegate")
def end_delegation(
    name: str, session: Session = Depends(get_session), by: str = "api"
) -> dict[str, Any]:
    try:
        participant = workflow.get_participant(session, name)
    except workflow.WorkflowError as exc:
        raise HTTPException(404, str(exc)) from exc
    ended = teams_module.revoke_delegation(session, participant, by=by)
    session.commit()
    return {"participant": name, "ended": ended}


# ---------------------------------------------------------------- routing


@app.post("/runs/{run_id}/route")
def route_run(
    run_id: int, session: Session = Depends(get_session), assigned_by: str = "api"
) -> dict[str, Any]:
    """Send every unassigned change to the team that owns its rule."""
    rulebook = load_rulebook(get_settings().rulebook_path)
    result = workflow.route_by_rule_owner(session, run_id, rulebook, assigned_by=assigned_by)
    session.commit()
    return result


@app.get("/runs/{run_id}/my-queue")
def my_queue(
    run_id: int, participant: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """What one participant should be looking at: their own work, their teams' pools,
    and anything they are covering for somebody who is away."""
    try:
        who = workflow.get_participant(session, participant)
    except workflow.WorkflowError as exc:
        raise HTTPException(404, str(exc)) from exc

    change_ids = workflow.queue_for(session, run_id, who)
    items = [
        review.queue_item(session, session.get(Change, change_id))
        for change_id in change_ids
    ]
    return {"participant": participant, "total": len(items), "items": items}


# --------------------------------------------------------------- handoffs


@app.get("/changes/{change_id}/handoffs")
def change_handoffs(change_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Every movement of a change, oldest first."""
    rows = workflow.handoffs(session, change_id)
    return {
        "change_id": change_id,
        "state": workflow.change_state(session, change_id),
        "history": [
            {
                "kind": row.kind.value,
                "actor": row.actor,
                "reason": row.reason,
                "at": row.at.isoformat(),
                "to_participant": (
                    session.get(Participant, row.to_participant_id).name
                    if row.to_participant_id else None
                ),
                "to_team": (
                    session.get(Team, row.to_team_id).slug if row.to_team_id else None
                ),
                "resolves_handoff_id": row.resolves_handoff_id,
            }
            for row in rows
        ],
    }


def _handoff_endpoint(change_id: int, request, session: Session, operation: str):
    """Shared plumbing for the four handoff verbs."""
    change = session.get(Change, change_id)
    if change is None:
        raise HTTPException(404, f"no change {change_id}")
    try:
        actor = workflow.get_participant(session, request.actor)
        target_participant = (
            workflow.get_participant(session, request.to_participant)
            if getattr(request, "to_participant", None) else None
        )
        target_team = (
            teams_module.get_team(session, request.to_team)
            if getattr(request, "to_team", None) else None
        )
    except (workflow.WorkflowError, teams_module.TeamError) as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        if operation == "reassign":
            row = workflow.reassign(session, change.run_id, change_id, actor=actor,
                                    to_participant=target_participant, to_team=target_team,
                                    reason=request.reason)
        elif operation == "escalate":
            row = workflow.escalate(session, change.run_id, change_id, actor=actor,
                                    reason=request.reason, to=target_participant)
        elif operation == "return":
            row = workflow.return_for_clarification(
                session, change.run_id, change_id, actor=actor, reason=request.reason,
                to_participant=target_participant, to_team=target_team,
            )
        else:
            row = workflow.resolve_return(session, change.run_id, change_id, actor=actor,
                                          answer=request.reason)
    except workflow.WorkflowError as exc:
        raise HTTPException(400, str(exc)) from exc

    session.commit()
    return {
        "change_id": change_id,
        "kind": row.kind.value,
        "actor": row.actor,
        "reason": row.reason,
        "state": workflow.change_state(session, change_id),
        "to": (
            session.get(Participant, row.to_participant_id).name
            if row.to_participant_id
            else (session.get(Team, row.to_team_id).slug if row.to_team_id else None)
        ),
    }


@app.post("/changes/{change_id}/reassign")
def reassign_change(change_id: int, request: HandoffRequest,
                    session: Session = Depends(get_session)) -> dict[str, Any]:
    """Move a change to another person or team, with a recorded reason."""
    return _handoff_endpoint(change_id, request, session, "reassign")


@app.post("/changes/{change_id}/escalate")
def escalate_change(change_id: int, request: HandoffRequest,
                    session: Session = Depends(get_session)) -> dict[str, Any]:
    """Send a change up to a team lead. It stays open and visibly escalated."""
    return _handoff_endpoint(change_id, request, session, "escalate")


@app.post("/changes/{change_id}/return")
def return_change(change_id: int, request: HandoffRequest,
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    """Send a change back with a question. It leaves the review queue until answered."""
    return _handoff_endpoint(change_id, request, session, "return")


@app.post("/changes/{change_id}/resolve-return")
def resolve_change_return(change_id: int, request: HandoffRequest,
                          session: Session = Depends(get_session)) -> dict[str, Any]:
    """Answer an outstanding return so the change re-enters the queue."""
    return _handoff_endpoint(change_id, request, session, "resolve")


# ------------------------------------------------------------- rule intake
#
# Sources in, candidate rules out, a person in between. Nothing here writes to the
# rulebook except /candidates/{id}/accept, and that is a deliberate human act.


@app.post("/sources/upload", status_code=201)
async def upload_source(
    file: UploadFile = File(...),
    uploaded_by: str = Form("api"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Take in a document, image or video and mine it for candidate rules.

    A source that cannot be fully read still returns 201 with ``needs`` populated. The
    upload succeeded; the extraction is what is incomplete, and saying so is more useful
    than an error that hides the fact the file is now on record.
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    settings = get_settings()
    try:
        source, _ = intake.ingest_file(
            session, data, file.filename or "upload",
            uploaded_by=uploaded_by,
            rulebook=load_rulebook(settings.rulebook_path),
            store=get_store(settings),
        )
    except intake.IntakeError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return intake.describe_source(session, source)


@app.post("/sources/url", status_code=201)
def fetch_source(
    request: SourceUrlRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Fetch a web page and mine it. The URL and retrieval time are the provenance."""
    settings = get_settings()
    try:
        source, _ = intake.ingest_url(
            session, request.url, uploaded_by=request.uploaded_by,
            rulebook=load_rulebook(settings.rulebook_path),
        )
    except intake.IntakeError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return intake.describe_source(session, source)


@app.get("/sources")
def list_sources(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = session.exec(select(RuleSource).order_by(RuleSource.id.desc())).all()  # type: ignore[union-attr]
    return [intake.describe_source(session, row) for row in rows]


@app.get("/candidates")
def list_candidates(
    session: Session = Depends(get_session),
    source_id: int | None = None,
    status: str = "proposed",
) -> dict[str, Any]:
    """Candidate rules awaiting review, best-evidenced first."""
    statement = select(RuleCandidate)
    if status != "all":
        statement = statement.where(RuleCandidate.status == status)
    if source_id is not None:
        statement = statement.where(RuleCandidate.source_id == source_id)
    rows = session.exec(
        statement.order_by(RuleCandidate.confidence.desc(), RuleCandidate.id)  # type: ignore[union-attr]
    ).all()
    return {
        "total": len(rows),
        "items": [intake.candidate_payload(session, row) for row in rows],
    }


@app.post("/candidates/{candidate_id}/accept")
def accept_candidate(
    candidate_id: int, request: AcceptCandidateRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Turn a candidate into a real rule.

    This is the one endpoint that changes what "correct" means, so it reports the hash
    moving and which existing runs that invalidates.
    """
    settings = get_settings()
    try:
        candidate, rulebook = intake.accept(
            session, candidate_id, request.reviewer,
            rulebook_path=settings.rulebook_path,
            owner=request.owner, overrides=request.overrides or None, note=request.note,
        )
    except intake.IntakeError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {
        "candidate_id": candidate_id,
        "status": candidate.status.value,
        "rule_id": candidate.rule_id,
        "rulebook_hash": rulebook.hash,
        "rules": len(rulebook),
        "runs_needing_rerun": intake.pending_reruns(session, rulebook),
    }


@app.post("/candidates/{candidate_id}/reject")
def reject_candidate(
    candidate_id: int, request: RejectCandidateRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        candidate = intake.reject(session, candidate_id, request.reviewer,
                                  note=request.note)
    except intake.IntakeError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {"candidate_id": candidate_id, "status": candidate.status.value}


@app.get("/rulebook/stale-runs")
def stale_runs(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Runs produced under a rulebook hash that is no longer current."""
    return intake.pending_reruns(session, load_rulebook(get_settings().rulebook_path))


# ------------------------------------------------------------ static frontend
#
# In development the dashboard is served by Vite on :5173 and proxies here. In a
# container the built assets sit alongside the API and are served from the same origin,
# so there is no CORS configuration and no separate service to deploy.


def _mount_frontend() -> None:
    dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if not dist.is_dir():
        return

    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        """Serve the SPA shell for any unmatched path so client-side routes deep-link."""
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


_mount_frontend()
