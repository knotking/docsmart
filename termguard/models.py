"""Layer S (substrate): the persistent schema.

Three groups of tables:

* **Lifecycle** - :class:`Document` and :class:`DocumentVersion` answer "what did this
  file look like at every point, and what produced each state". A version is immutable:
  content is identified by SHA-256 and stored in the object store. Versions form a chain
  via ``parent_version_id``, so lineage is walkable in both directions.
* **Pipeline** - :class:`Run`, :class:`Hit`, :class:`Change`, :class:`Decision` record what
  the scanner found, what was proposed, by which mechanism, and how a reviewer ruled.
* **Audit** - :class:`AuditEvent` is the append-only narrative spine. Everything above
  also emits an event here, so a single ordered table can be exported as the audit trail.

Append-only discipline (constraint 4): rows in ``Change``, ``Decision`` and ``AuditEvent``
are never updated or deleted. A superseding decision is a new ``Decision`` row; the latest
by ``decided_at`` wins. ``Document.current_version_id`` is the sole mutable pointer, and it
only ever moves forward along the version chain.

Types are chosen to be portable between SQLite and Postgres (constraint 9): no dialect
specific column types, JSON payloads via SQLAlchemy's generic ``JSON``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, Column, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Timezone-aware UTC now. All timestamps in this schema are UTC."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- enums


class Stage(str, Enum):
    """Where a document version sits in its life."""

    INGESTED = "ingested"      # original bytes as received; never edited
    REDLINED = "redlined"      # tracked changes applied, awaiting review
    REVIEWED = "reviewed"      # every change has a decision
    FINAL = "final"            # decisions resolved into clean text
    VERIFIED = "verified"      # re-scanned clean and diff-explained


class Mechanism(str, Enum):
    DETERMINISTIC = "deterministic"
    AI = "ai"


class Classification(str, Enum):
    UNAMBIGUOUS = "unambiguous"
    NEEDS_JUDGMENT = "needs_judgment"


class DecisionKind(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"


class ChangeStatus(str, Enum):
    PROPOSED = "proposed"      # written into the redlined docx as a tracked change
    COMMENTED = "commented"    # comment only, no text change (LLM said keep/escalate)
    DECIDED = "decided"        # a reviewer has ruled


class RunStatus(str, Enum):
    RUNNING = "running"
    SCANNED = "scanned"
    REDLINED = "redlined"
    JUDGED = "judged"
    COMPLETE = "complete"
    VERIFIED = "verified"
    FAILED = "failed"


class ActorKind(str, Enum):
    HUMAN = "human"
    RULE_ENGINE = "rule_engine"
    LLM = "llm"
    SYSTEM = "system"


# ----------------------------------------------------------------------- lifecycle


class Document(SQLModel, table=True):
    """A logical document, stable across all its versions.

    Identity is the source filename within a corpus. ``current_version_id`` is a cursor
    into the version chain, not a store of content.
    """

    __tablename__ = "document"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, description="source filename, e.g. IFU-004.docx")
    doc_number: Optional[str] = Field(default=None, index=True)
    doc_type: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    current_version_id: Optional[int] = Field(default=None, foreign_key="document_version.id")
    origin_uri: Optional[str] = Field(default=None, description="where it was ingested from")

    __table_args__ = (UniqueConstraint("name", name="uq_document_name"),)


class DocumentVersion(SQLModel, table=True):
    """One immutable state of a document.

    ``content_sha256`` is both the object-store address and the integrity proof. Two
    versions with the same hash are byte-identical, which is how a no-op stage is
    detected. ``parent_version_id`` makes the chain walkable.
    """

    __tablename__ = "document_version"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id", index=True)
    version_no: int = Field(index=True, description="1-based, monotonic per document")
    stage: Stage = Field(index=True)

    content_sha256: str = Field(index=True)
    blob_uri: str
    size_bytes: int = 0

    parent_version_id: Optional[int] = Field(default=None, foreign_key="document_version.id")
    run_id: Optional[int] = Field(default=None, foreign_key="run.id", index=True)

    actor: str = Field(description="who or what produced this version")
    actor_kind: ActorKind = Field(default=ActorKind.SYSTEM)
    created_at: datetime = Field(default_factory=utcnow, index=True)

    note: Optional[str] = Field(default=None, sa_column=Column(Text))
    # Per-version rollup: {"deterministic": 7, "ai": 2, "hits": 9, ...}
    summary: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_version_per_document"),
        Index("ix_version_doc_stage", "document_id", "stage"),
    )


# ------------------------------------------------------------------------ pipeline


class Run(SQLModel, table=True):
    """One execution of the pipeline over a corpus."""

    __tablename__ = "run"

    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: Optional[datetime] = None
    status: RunStatus = Field(default=RunStatus.RUNNING, index=True)

    rulebook_hash: str = ""
    corpus_hash: str = ""
    corpus_dir: str = ""
    dry_run: bool = True
    actor: str = "system"
    note: Optional[str] = Field(default=None, sa_column=Column(Text))
    stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Hit(SQLModel, table=True):
    """One rule match at one location, as found by the scanner."""

    __tablename__ = "hit"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    document_id: int = Field(foreign_key="document.id", index=True)
    document_version_id: int = Field(foreign_key="document_version.id", index=True)

    rule_id: str = Field(index=True)
    classification: Classification = Field(index=True)
    reason: str = Field(default="", description="why it was classified this way")

    part: str = Field(index=True, description="body|header|footer|footnote|endnote|textbox")
    location: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    paragraph_ref: Optional[str] = None
    paragraph_index: int = 0
    is_heading: bool = False

    matched_text: str = ""
    approved_text: str = ""
    span_start: int = 0
    span_end: int = 0
    sentence: str = Field(default="", sa_column=Column(Text))
    paragraph_text: str = Field(default="", sa_column=Column(Text))
    occurrence: int = Field(default=0, description="0-based index of this match within its paragraph")

    created_at: datetime = Field(default_factory=utcnow)


class Change(SQLModel, table=True):
    """A proposed edit derived from a hit. Append-only."""

    __tablename__ = "change"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    hit_id: int = Field(foreign_key="hit.id", index=True)
    document_id: int = Field(foreign_key="document.id", index=True)
    document_version_id: Optional[int] = Field(
        default=None, foreign_key="document_version.id", index=True,
        description="the redlined version this change was written into",
    )

    mechanism: Mechanism = Field(index=True)
    status: ChangeStatus = Field(default=ChangeStatus.PROPOSED, index=True)

    original_text: str = Field(default="", sa_column=Column(Text))
    proposed_text: str = Field(default="", sa_column=Column(Text))
    comment: str = Field(default="", sa_column=Column(Text))

    # AI provenance - null for deterministic changes
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    prompt_hash: Optional[str] = None
    request_id: Optional[str] = None
    latency_ms: Optional[int] = None
    justification: Optional[str] = Field(default=None, sa_column=Column(Text))
    llm_decision: Optional[str] = Field(default=None, description="change|keep|escalate")

    # anchor back into the .docx
    revision_id: Optional[int] = None
    comment_id: Optional[int] = None

    applied_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


class Decision(SQLModel, table=True):
    """A reviewer ruling on a change. Append-only: supersede by inserting a newer row."""

    __tablename__ = "decision"

    id: Optional[int] = Field(default=None, primary_key=True)
    change_id: int = Field(foreign_key="change.id", index=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    decision: DecisionKind = Field(index=True)
    reviewer: str = Field(index=True)
    final_text: Optional[str] = Field(default=None, sa_column=Column(Text))
    note: Optional[str] = Field(default=None, sa_column=Column(Text))
    decided_at: datetime = Field(default_factory=utcnow, index=True)


# --------------------------------------------------------------------------- audit


class AuditEvent(SQLModel, table=True):
    """Append-only event log. The exportable audit trail is an ordered read of this table.

    Deliberately denormalized: every event carries enough identity to be read on its own,
    because an auditor reads a CSV, not a join.
    """

    __tablename__ = "audit_event"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    event: str = Field(index=True, description="e.g. document.ingested, change.proposed")

    run_id: Optional[int] = Field(default=None, foreign_key="run.id", index=True)
    document_id: Optional[int] = Field(default=None, foreign_key="document.id", index=True)
    document_version_id: Optional[int] = Field(default=None, foreign_key="document_version.id", index=True)
    hit_id: Optional[int] = Field(default=None, foreign_key="hit.id")
    change_id: Optional[int] = Field(default=None, foreign_key="change.id", index=True)

    actor: str = "system"
    actor_kind: ActorKind = Field(default=ActorKind.SYSTEM, index=True)
    mechanism: Optional[Mechanism] = Field(default=None, index=True)
    rule_id: Optional[str] = Field(default=None, index=True)

    summary: str = Field(default="", sa_column=Column(Text))
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    content_sha256: Optional[str] = Field(default=None, description="content this event concerns")
