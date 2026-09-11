"""Layer S (substrate): document lifecycle and version chain.

The single writer of :class:`DocumentVersion` rows (constraint 7). Every other module asks
this one to record a new state; none of them fabricate versions or touch blobs directly.

A document's life is a chain of immutable versions::

    v1 ingested  --(scan+redline)-->  v2 redlined  --(review)-->  v3 reviewed
                                                   --(resolve)-->  v4 final
                                                   --(re-scan)-->  v5 verified

Each version records the SHA-256 of its bytes, the object-store URI, its parent, the run
that produced it, and the actor responsible. Because content is addressed by hash, a stage
that changes nothing is detectable (same hash as parent) and stored once.

What this buys, concretely: for any document you can answer *what it looked like at any
point* (fetch the blob by hash), *what changed between two points* (diff two versions),
*what produced the change* (run + actor + the changes linked to that version), and *that
nothing has silently rotted* (re-hash the bytes and compare).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlmodel import Session, func, select

from termguard import audit
from termguard.models import (
    ActorKind,
    Change,
    Document,
    DocumentVersion,
    Stage,
)
from termguard.storage import ObjectStore, sha256_bytes


# ------------------------------------------------------------------ identity


def get_or_create_document(
    session: Session,
    name: str,
    *,
    doc_number: str | None = None,
    doc_type: str | None = None,
    origin_uri: str | None = None,
) -> Document:
    """Find the document with this source name, or create it."""
    doc = session.exec(select(Document).where(Document.name == name)).first()
    if doc is not None:
        return doc
    doc = Document(name=name, doc_number=doc_number, doc_type=doc_type, origin_uri=origin_uri)
    session.add(doc)
    session.flush()
    return doc


# ------------------------------------------------------------------ versions


def add_version(
    session: Session,
    store: ObjectStore,
    document: Document,
    data: bytes,
    stage: Stage,
    *,
    actor: str,
    actor_kind: ActorKind = ActorKind.SYSTEM,
    parent: DocumentVersion | None = None,
    run_id: int | None = None,
    note: str | None = None,
    summary: dict[str, Any] | None = None,
    advance_current: bool = True,
) -> DocumentVersion:
    """Store ``data`` and record it as the document's next version.

    Idempotent on content *within a stage*: re-recording identical bytes at the same stage
    with the same parent returns the existing version rather than growing the chain with
    duplicates. That keeps re-runs from inflating history.
    """
    digest = store.put_bytes(data)

    existing = session.exec(
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.stage == stage,
            DocumentVersion.content_sha256 == digest,
            DocumentVersion.parent_version_id == (parent.id if parent else None),
        )
        .order_by(DocumentVersion.version_no.desc())  # type: ignore[union-attr]
    ).first()
    if existing is not None:
        return existing

    next_no = (
        session.exec(
            select(func.max(DocumentVersion.version_no)).where(
                DocumentVersion.document_id == document.id
            )
        ).one()
        or 0
    ) + 1

    version = DocumentVersion(
        document_id=document.id,  # type: ignore[arg-type]
        version_no=next_no,
        stage=stage,
        content_sha256=digest,
        blob_uri=store.uri(digest),
        size_bytes=len(data),
        parent_version_id=parent.id if parent else None,
        run_id=run_id,
        actor=actor,
        actor_kind=actor_kind,
        note=note,
        summary=summary or {},
    )
    session.add(version)
    session.flush()

    if advance_current:
        document.current_version_id = version.id
        session.add(document)

    unchanged = parent is not None and parent.content_sha256 == digest
    audit.record(
        session,
        f"version.{stage.value}",
        summary=(
            f"{document.name} v{next_no} ({stage.value})"
            + (" - content unchanged from parent" if unchanged else "")
        ),
        actor=actor,
        actor_kind=actor_kind,
        run_id=run_id,
        document_id=document.id,
        document_version_id=version.id,
        content_sha256=digest,
        payload={
            "version_no": next_no,
            "stage": stage.value,
            "parent_version_no": parent.version_no if parent else None,
            "size_bytes": len(data),
            "blob_uri": version.blob_uri,
            "content_unchanged": unchanged,
            **({"note": note} if note else {}),
            **(summary or {}),
        },
    )
    return version


def ingest(
    session: Session,
    store: ObjectStore,
    path: Path,
    *,
    actor: str = "system",
    actor_kind: ActorKind = ActorKind.SYSTEM,
    run_id: int | None = None,
    doc_number: str | None = None,
    doc_type: str | None = None,
) -> tuple[Document, DocumentVersion]:
    """Bring a file under management as an ``ingested`` version.

    The original file on disk is never touched again after this read; everything
    downstream works from the stored blob.
    """
    path = Path(path)
    data = path.read_bytes()
    doc = get_or_create_document(
        session, path.name, doc_number=doc_number, doc_type=doc_type,
        origin_uri=str(path.resolve()),
    )
    existing = latest_at_stage(session, doc.id, Stage.INGESTED)  # type: ignore[arg-type]
    if existing is not None and existing.content_sha256 == sha256_bytes(data):
        return doc, existing  # already ingested, byte-identical

    version = add_version(
        session, store, doc, data, Stage.INGESTED,
        actor=actor, actor_kind=actor_kind, parent=existing, run_id=run_id,
        note="original as received" if existing is None else "re-ingested, content changed",
    )
    return doc, version


# ------------------------------------------------------------------ queries


def history(session: Session, document_id: int) -> Sequence[DocumentVersion]:
    """Every version of a document, oldest first."""
    return session.exec(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_no)
    ).all()


def latest(session: Session, document_id: int) -> DocumentVersion | None:
    """The newest version of a document."""
    return session.exec(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_no.desc())  # type: ignore[union-attr]
    ).first()


def latest_at_stage(session: Session, document_id: int, stage: Stage) -> DocumentVersion | None:
    """The newest version of a document at a given stage."""
    return session.exec(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id, DocumentVersion.stage == stage)
        .order_by(DocumentVersion.version_no.desc())  # type: ignore[union-attr]
    ).first()


def lineage(session: Session, version_id: int) -> list[DocumentVersion]:
    """Walk parent links from a version back to the root. Returns oldest first."""
    chain: list[DocumentVersion] = []
    seen: set[int] = set()
    current = session.get(DocumentVersion, version_id)
    while current is not None and current.id not in seen:
        chain.append(current)
        seen.add(current.id)  # type: ignore[arg-type]
        current = (
            session.get(DocumentVersion, current.parent_version_id)
            if current.parent_version_id
            else None
        )
    return list(reversed(chain))


def content(store: ObjectStore, version: DocumentVersion) -> bytes:
    """The exact bytes of a version."""
    return store.get_bytes(version.content_sha256)


def changes_for_version(session: Session, version_id: int) -> Sequence[Change]:
    """Every change written into a given version."""
    return session.exec(
        select(Change).where(Change.document_version_id == version_id).order_by(Change.id)
    ).all()


# ------------------------------------------------------------------ integrity


def verify_integrity(
    session: Session, store: ObjectStore, document_id: int | None = None
) -> dict[str, Any]:
    """Re-hash stored bytes for every version and confirm they match their recorded digest.

    A cheap, honest answer to "can you prove the archive has not been tampered with".
    """
    stmt = select(DocumentVersion)
    if document_id is not None:
        stmt = stmt.where(DocumentVersion.document_id == document_id)
    versions = session.exec(stmt.order_by(DocumentVersion.id)).all()

    failures: list[dict[str, Any]] = []
    for v in versions:
        try:
            actual = sha256_bytes(store.get_bytes(v.content_sha256))
            if actual != v.content_sha256:
                failures.append(
                    {"version_id": v.id, "expected": v.content_sha256, "actual": actual,
                     "problem": "content does not match digest"}
                )
        except KeyError:
            failures.append(
                {"version_id": v.id, "expected": v.content_sha256, "actual": None,
                 "problem": "blob missing from store"}
            )
    return {"checked": len(versions), "ok": not failures, "failures": failures}


def document_timeline(session: Session, document_id: int) -> list[dict[str, Any]]:
    """A flat, readable life story of one document: versions and the events between them."""
    rows: list[dict[str, Any]] = []
    for v in history(session, document_id):
        rows.append(
            {
                "kind": "version",
                "version_no": v.version_no,
                "stage": v.stage.value,
                "sha256": v.content_sha256,
                "short_sha": v.content_sha256[:12],
                "actor": v.actor,
                "actor_kind": v.actor_kind.value,
                "run_id": v.run_id,
                "at": v.created_at.isoformat(),
                "size_bytes": v.size_bytes,
                "note": v.note,
                "summary": v.summary,
                "parent_version_id": v.parent_version_id,
            }
        )
    for e in audit.events_for_document(session, document_id):
        rows.append(
            {
                "kind": "event",
                "event": e.event,
                "summary": e.summary,
                "actor": e.actor,
                "actor_kind": e.actor_kind.value,
                "run_id": e.run_id,
                "at": e.ts.isoformat(),
                "rule_id": e.rule_id,
                "mechanism": e.mechanism.value if e.mechanism else None,
                "payload": e.payload,
            }
        )
    rows.sort(key=lambda r: (r["at"], 0 if r["kind"] == "version" else 1))
    return rows
