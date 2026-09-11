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

    # anchor back into the .docx. revision_ids holds every w:id this change wrote
    # (the w:del and its paired w:ins), which is what verify.py resolves against.
    revision_id: Optional[int] = None
    revision_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON))
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

    # Who decided, and - when it was an agent - on whose authority. A decision with
    # decided_by_kind == AGENT and no policy_clause_id is a bug, and verify.py says so.
    participant_id: Optional[int] = Field(default=None, foreign_key="participant.id", index=True)
    decided_by_kind: ActorKind = Field(default=ActorKind.HUMAN, index=True)
    policy_clause_id: Optional[str] = Field(default=None, index=True)
    policy_hash: Optional[str] = None
    requires_human_confirm: bool = Field(default=False, index=True)
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None


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


# ------------------------------------------------------------------- workflow
#
# Several people - and several agents - work one run together. Three things have to be
# true for that to be defensible in a regulated setting:
#
#   1. Every decision names a participant, and participants are typed: a human and an
#      agent are not interchangeable, and the trail must never blur them.
#   2. Two reviewers cannot silently decide the same change. Work is assigned, and held
#      under a short lease while someone is actually on it.
#   3. Separation of duties. Whoever authored content cannot be the one who approves it,
#      and no agent signs anything off.


class Role(str, Enum):
    """What a participant is entitled to do. A participant may hold several."""

    REVIEWER = "reviewer"    # decides individual changes
    APPROVER = "approver"    # signs off a whole run; may not have decided in it
    AUTHOR = "author"        # proposes content; explicitly cannot approve
    OBSERVER = "observer"    # read-only


class ParticipantKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"


class ClaimStatus(str, Enum):
    HELD = "held"
    RELEASED = "released"
    EXPIRED = "expired"


class SignOffDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class Participant(SQLModel, table=True):
    """A human or an agent that can act on a run.

    Identity is asserted by the caller, not proven - authentication is deliberately out of
    scope (CLAUDE.md) and IAM in front of the service is the real boundary. What this
    table buys is *attribution*: every decision, claim and sign-off resolves to a row here
    with a kind and a set of roles, so the audit trail can never report an agent decision
    as if a person made it.
    """

    __tablename__ = "participant"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    email: Optional[str] = Field(default=None, index=True)
    kind: ParticipantKind = Field(default=ParticipantKind.HUMAN, index=True)
    roles: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utcnow)

    # Agents only: what is running, and under which authority.
    model: Optional[str] = None
    policy_hash: Optional[str] = None
    note: Optional[str] = Field(default=None, sa_column=Column(Text))

    __table_args__ = (UniqueConstraint("name", name="uq_participant_name"),)

    def has_role(self, role: Role | str) -> bool:
        return (role.value if isinstance(role, Role) else role) in self.roles

    @property
    def is_agent(self) -> bool:
        return self.kind is ParticipantKind.AGENT


class Assignment(SQLModel, table=True):
    """Durable routing: whose queue a change sits in.

    Append-only, like everything else that records intent. Reassigning inserts a row; the
    newest row for a change is the live one, and the history shows a change being handed
    between reviewers rather than quietly moving.
    """

    __tablename__ = "assignment"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    change_id: int = Field(foreign_key="change.id", index=True)

    # Exactly one of these is the owner. A team assignment is a pool any member may
    # claim; a participant assignment names one person. `pinned` marks a person
    # assignment that a re-route must not quietly overwrite.
    participant_id: Optional[int] = Field(default=None, foreign_key="participant.id", index=True)
    team_id: Optional[int] = Field(default=None, foreign_key="team.id", index=True)
    pinned: bool = Field(default=False, index=True)

    assigned_by: str = Field(default="system")
    assigned_at: datetime = Field(default_factory=utcnow, index=True)
    reason: Optional[str] = Field(default=None, description="why this owner, e.g. 'rule owner'")


class Claim(SQLModel, table=True):
    """A short lease held while someone is actually working on a change.

    Assignment says a change is yours; a claim says you are on it *now*. Without this,
    two reviewers working the same queue both open change 412, both decide, and the second
    decision silently supersedes the first - which the append-only log would faithfully
    record and nobody would ever notice.

    Leases expire so a reviewer who closes their laptop does not block the queue.
    """

    __tablename__ = "claim"

    id: Optional[int] = Field(default=None, primary_key=True)
    change_id: int = Field(foreign_key="change.id", index=True)
    participant_id: int = Field(foreign_key="participant.id", index=True)
    status: ClaimStatus = Field(default=ClaimStatus.HELD, index=True)
    claimed_at: datetime = Field(default_factory=utcnow, index=True)
    expires_at: datetime = Field(index=True)
    released_at: Optional[datetime] = None

    __table_args__ = (Index("ix_claim_change_status", "change_id", "status"),)


class SignOff(SQLModel, table=True):
    """A participant approving or rejecting a whole run.

    Records exactly what was signed: the rulebook and corpus hashes, the policy hash under
    which any agent decisions were made, and the counts at the moment of signing. A
    sign-off that cannot say what it covered is not a sign-off.
    """

    __tablename__ = "signoff"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    participant_id: int = Field(foreign_key="participant.id", index=True)
    decision: SignOffDecision = Field(index=True)
    role: Role = Field(default=Role.APPROVER)
    signed_at: datetime = Field(default_factory=utcnow, index=True)
    note: Optional[str] = Field(default=None, sa_column=Column(Text))

    rulebook_hash: str = ""
    corpus_hash: str = ""
    policy_hash: str = ""
    covered: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


# ----------------------------------------------------------- org and teams
#
# Structure, not tenancy. Organizations and teams group people and route work; no query
# is scoped by them, and the CLAUDE.md "no multi-tenancy" constraint stands. IAM in front
# of the service is still the real access boundary.
#
# Team slugs match the `owner` field the rulebook already carries on every rule, so work
# routes to the team that owns the rule without anyone maintaining a second mapping.


class TeamRole(str, Enum):
    MEMBER = "member"
    LEAD = "lead"      # escalation target for the team


class HandoffKind(str, Enum):
    """How a change moved. Every movement is recorded; none of them mutate the change."""

    ROUTE = "route"          # first placement, from the rule's owning team
    REASSIGN = "reassign"    # moved to another person or team, with a reason
    ESCALATE = "escalate"    # a reviewer could not decide; sent to the team lead
    RETURN = "return"        # sent back for clarification; leaves the review queue
    RESOLVE = "resolve"      # a return was answered; the change re-enters the queue
    DELEGATE = "delegate"    # covered while the owner is away


class Organization(SQLModel, table=True):
    __tablename__ = "organization"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)

    __table_args__ = (UniqueConstraint("slug", name="uq_org_slug"),)


class Team(SQLModel, table=True):
    """A functional group. ``slug`` is the join to the rulebook's ``owner`` field."""

    __tablename__ = "team"

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="organization.id", index=True)
    name: str
    slug: str = Field(index=True, description="matches a rulebook rule's `owner`")
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)

    # Globally unique, not per-org: routing resolves a team by slug alone, so two orgs
    # holding the same slug would make every lookup ambiguous. Orgs are structure here,
    # not tenancy, so one team per slug per deployment is the honest constraint.
    __table_args__ = (UniqueConstraint("slug", name="uq_team_slug"),)


class Membership(SQLModel, table=True):
    """A participant's place in a team. Leaving sets ``left_at`` rather than deleting."""

    __tablename__ = "membership"

    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    participant_id: int = Field(foreign_key="participant.id", index=True)
    team_role: TeamRole = Field(default=TeamRole.MEMBER, index=True)
    joined_at: datetime = Field(default_factory=utcnow)
    left_at: Optional[datetime] = Field(default=None, index=True)

    __table_args__ = (Index("ix_membership_team_active", "team_id", "left_at"),)


class Delegation(SQLModel, table=True):
    """Cover while someone is away.

    A delegation does not move any assignment. It changes who the *effective* owner of a
    change is while it is in force, and lapses on its own. Reassigning a hundred changes
    because someone took a week off, then reassigning them back, is how work gets lost.
    """

    __tablename__ = "delegation"

    id: Optional[int] = Field(default=None, primary_key=True)
    participant_id: int = Field(foreign_key="participant.id", index=True)
    delegate_id: int = Field(foreign_key="participant.id", index=True)
    starts_at: datetime = Field(default_factory=utcnow, index=True)
    ends_at: Optional[datetime] = Field(default=None, index=True)
    reason: Optional[str] = None
    revoked_at: Optional[datetime] = None
    created_by: str = "system"


class Handoff(SQLModel, table=True):
    """One movement of a change between owners. Append-only.

    The reason is required for every kind except the initial route. Work changing hands
    without a recorded reason is exactly the thing an auditor asks about, and "the system
    moved it" is not an answer.
    """

    __tablename__ = "handoff"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    change_id: int = Field(foreign_key="change.id", index=True)
    kind: HandoffKind = Field(index=True)

    from_participant_id: Optional[int] = Field(default=None, foreign_key="participant.id")
    from_team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    to_participant_id: Optional[int] = Field(default=None, foreign_key="participant.id", index=True)
    to_team_id: Optional[int] = Field(default=None, foreign_key="team.id", index=True)

    actor: str = Field(index=True)
    reason: Optional[str] = Field(default=None, sa_column=Column(Text))
    at: datetime = Field(default_factory=utcnow, index=True)

    # A RETURN is answered by a later RESOLVE naming it.
    resolves_handoff_id: Optional[int] = Field(default=None, foreign_key="handoff.id", index=True)

    __table_args__ = (Index("ix_handoff_change_at", "change_id", "at"),)


# ------------------------------------------------------------- rule intake
#
# Sources uploaded or fetched to mine terminology from, and the candidate rules they
# yield. Nothing here touches the rulebook directly: a candidate is a proposal carrying
# the sentence it came from, and a person accepts it. The same shape as everything else
# in this system, for the same reason - a wrong rule does not fail loudly, it quietly
# rewrites correct text across every document on the next run.


class SourceKind(str, Enum):
    DOCUMENT = "document"
    IMAGE = "image"
    VIDEO = "video"
    WEB = "web"


class CandidateStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    EDITED = "edited"      # accepted with the reviewer's own wording
    REJECTED = "rejected"


class RuleSource(SQLModel, table=True):
    """Something a rule was mined from, kept so a rule can be traced back to it."""

    __tablename__ = "rule_source"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    kind: SourceKind = Field(index=True)
    origin: str = Field(description="filename or URL")
    content_sha256: Optional[str] = Field(default=None, index=True)
    blob_uri: Optional[str] = None

    uploaded_by: str = Field(default="system", index=True)
    uploaded_at: datetime = Field(default_factory=utcnow, index=True)
    retrieved_at: Optional[str] = None

    lines_read: int = 0
    candidates_found: int = 0
    # What was missing to read this fully - an API key for an image, a transcript for a
    # video. Recorded rather than silently returning nothing.
    needs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    notes: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class RuleCandidate(SQLModel, table=True):
    """A proposed rule with the line that produced it. Reviewed before it applies."""

    __tablename__ = "rule_candidate"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="rule_source.id", index=True)

    deprecated: str = Field(index=True)
    approved: str
    method: str = Field(description="the extraction pattern, or 'model'")
    confidence: float = 0.5

    # Provenance. The reviewer checks the quote, not the rule.
    quote: str = Field(default="", sa_column=Column(Text))
    locator: str = ""
    note: Optional[str] = Field(default=None, sa_column=Column(Text))
    warnings: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    suggested: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    status: CandidateStatus = Field(default=CandidateStatus.PROPOSED, index=True)
    decided_by: Optional[str] = Field(default=None, index=True)
    decided_at: Optional[datetime] = None
    decision_note: Optional[str] = Field(default=None, sa_column=Column(Text))
    rule_id: Optional[str] = Field(default=None, index=True,
                                   description="the rule id created on acceptance")
    created_at: datetime = Field(default_factory=utcnow, index=True)
