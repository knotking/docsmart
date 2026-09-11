"""Layer D: several participants - human and agent - working one run.

Three mechanisms, each solving a problem that only appears once more than one reviewer is
on the same queue:

**Assignment** routes a change into someone's queue. Append-only, so a change being
handed from one reviewer to another is visible rather than silent.

**Claims** are short leases held while somebody is actually on a change. Without them, two
reviewers open change 412, both decide, and the append-only log faithfully records the
second decision superseding the first with nobody the wiser. A claim makes that collision
an error instead of a silent overwrite, and leases expire so a closed laptop does not
block the queue.

**Separation of duties** is the maker-checker rule. Whoever authored content in a run
cannot be the one who signs it off, and no agent signs anything off, ever. These are
enforced here rather than documented as a convention, because a convention is not a
control.

Agents participate through the same machinery. An agent decides only what
:mod:`termguard.policy` authorizes, its decisions carry the clause id that permitted them,
and a clause may demand that a human confirm the batch before the run can be signed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable, Sequence

from sqlmodel import Session, func, select

from termguard import audit
from termguard.models import (
    ActorKind,
    Assignment,
    Change,
    ChangeStatus,
    Claim,
    ClaimStatus,
    Decision,
    DecisionKind,
    Document,
    Hit,
    Mechanism,
    Participant,
    ParticipantKind,
    Role,
    Run,
    SignOff,
    SignOffDecision,
    utcnow,
)
from termguard.policy import Clause, Policy

DEFAULT_LEASE_SECONDS = 600


class WorkflowError(RuntimeError):
    """A workflow rule was violated. The message is written to be shown to the user."""


class ClaimConflict(WorkflowError):
    """Another participant holds the lease on this change."""


# ------------------------------------------------------------- participants


def ensure_participant(
    session: Session,
    name: str,
    *,
    kind: ParticipantKind | str = ParticipantKind.HUMAN,
    roles: Sequence[Role | str] = (Role.REVIEWER,),
    email: str | None = None,
    model: str | None = None,
    policy_hash: str | None = None,
    note: str | None = None,
) -> Participant:
    """Find or create a participant. Roles are widened on an existing row, never narrowed.

    Widening only: revoking a role is a deliberate administrative act, not something a
    routine call should do by omission.
    """
    wanted = [r.value if isinstance(r, Role) else r for r in roles]
    existing = session.exec(select(Participant).where(Participant.name == name)).first()
    if existing is not None:
        added = [r for r in wanted if r not in existing.roles]
        if added:
            existing.roles = [*existing.roles, *added]
            session.add(existing)
            session.flush()
            audit.record(
                session, "participant.roles_granted",
                summary=f"{name} granted {', '.join(added)}",
                actor="system", payload={"roles": existing.roles, "added": added},
            )
        return existing

    participant = Participant(
        name=name,
        email=email,
        kind=ParticipantKind(kind) if isinstance(kind, str) else kind,
        roles=wanted,
        model=model,
        policy_hash=policy_hash,
        note=note,
    )
    session.add(participant)
    session.flush()
    audit.record(
        session, "participant.registered",
        summary=f"{participant.kind.value} {name} registered as {', '.join(wanted)}",
        actor="system",
        actor_kind=ActorKind.LLM if participant.is_agent else ActorKind.HUMAN,
        payload={"kind": participant.kind.value, "roles": wanted, "model": model,
                 "policy_hash": policy_hash},
    )
    return participant


def get_participant(session: Session, identifier: int | str) -> Participant:
    """Look a participant up by id or name. Raises if unknown - never invents one."""
    participant = (
        session.get(Participant, identifier)
        if isinstance(identifier, int)
        else session.exec(select(Participant).where(Participant.name == identifier)).first()
    )
    if participant is None:
        raise WorkflowError(f"unknown participant {identifier!r}")
    return participant


def require_role(participant: Participant, role: Role) -> None:
    if not participant.has_role(role):
        raise WorkflowError(
            f"{participant.name} does not hold the {role.value} role "
            f"(has: {', '.join(participant.roles) or 'none'})"
        )


# -------------------------------------------------------------- assignment


def assign(
    session: Session,
    run_id: int,
    change_ids: Sequence[int],
    participant: Participant,
    *,
    assigned_by: str = "system",
    reason: str | None = None,
) -> list[Assignment]:
    """Route changes into a participant's queue."""
    require_role(participant, Role.REVIEWER)
    rows: list[Assignment] = []
    for change_id in change_ids:
        row = Assignment(
            run_id=run_id, change_id=change_id, participant_id=participant.id,  # type: ignore[arg-type]
            assigned_by=assigned_by, reason=reason,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    audit.record(
        session, "changes.assigned",
        summary=f"{len(rows)} change(s) assigned to {participant.name}"
                + (f" ({reason})" if reason else ""),
        actor=assigned_by, run_id=run_id,
        payload={"participant": participant.name, "count": len(rows), "reason": reason},
    )
    return rows


def current_assignee(session: Session, change_id: int) -> Participant | None:
    """The newest assignment for a change."""
    row = session.exec(
        select(Assignment)
        .where(Assignment.change_id == change_id)
        .order_by(Assignment.id.desc())  # type: ignore[union-attr]
    ).first()
    return session.get(Participant, row.participant_id) if row else None


def auto_assign(
    session: Session,
    run_id: int,
    participants: Sequence[Participant],
    *,
    strategy: str = "by_file",
    assigned_by: str = "system",
) -> dict[str, int]:
    """Spread a run's undecided changes across reviewers.

    ``by_file`` keeps a whole document with one reviewer, which matters more than even
    load: terminology decisions are contextual, and a reviewer who has read the document
    makes better and faster calls on the rest of it than one parachuted into paragraph 14.
    ``by_rule`` groups by rule instead, for a specialist clearing one rule across the
    corpus. ``round_robin`` balances count and ignores both.
    """
    if not participants:
        raise WorkflowError("no participants to assign to")

    pending = [c for c in _undecided_changes(session, run_id) if current_assignee(session, c.id) is None]
    if not pending:
        return {}

    buckets: dict[Any, list[int]] = {}
    for index, change in enumerate(pending):
        if strategy == "by_file":
            key = change.document_id
        elif strategy == "by_rule":
            hit = session.get(Hit, change.hit_id) if change.hit_id else None
            key = hit.rule_id if hit else "unknown"
        elif strategy == "round_robin":
            key = index
        else:
            raise WorkflowError(f"unknown assignment strategy {strategy!r}")
        buckets.setdefault(key, []).append(change.id)  # type: ignore[arg-type]

    # Largest bucket to the least-loaded reviewer, so one huge document does not land on
    # someone who already has the rest of the corpus.
    counts: dict[int, int] = {p.id: 0 for p in participants}  # type: ignore[misc]
    result: dict[str, int] = {p.name: 0 for p in participants}
    for _key, change_ids in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        target = min(participants, key=lambda p: counts[p.id])
        assign(session, run_id, change_ids, target, assigned_by=assigned_by,
               reason=f"auto ({strategy})")
        counts[target.id] += len(change_ids)
        result[target.name] += len(change_ids)
    return result


def _undecided_changes(session: Session, run_id: int) -> list[Change]:
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


# ------------------------------------------------------------------ claims


def _expire_stale(session: Session, change_id: int) -> None:
    now = utcnow()
    stale = session.exec(
        select(Claim).where(Claim.change_id == change_id, Claim.status == ClaimStatus.HELD)
    ).all()
    for claim in stale:
        expires = claim.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=now.tzinfo)
        if expires <= now:
            claim.status = ClaimStatus.EXPIRED
            session.add(claim)
    session.flush()


def active_claim(session: Session, change_id: int) -> Claim | None:
    """The live lease on a change, expiring any that have run out first."""
    _expire_stale(session, change_id)
    return session.exec(
        select(Claim)
        .where(Claim.change_id == change_id, Claim.status == ClaimStatus.HELD)
        .order_by(Claim.id.desc())  # type: ignore[union-attr]
    ).first()


def claim(
    session: Session,
    change_id: int,
    participant: Participant,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Claim:
    """Take the lease on a change. Re-claiming your own lease extends it."""
    require_role(participant, Role.REVIEWER)
    held = active_claim(session, change_id)
    if held is not None and held.participant_id != participant.id:
        other = session.get(Participant, held.participant_id)
        raise ClaimConflict(
            f"change {change_id} is held by {other.name if other else 'another reviewer'} "
            f"until {held.expires_at.isoformat()}"
        )

    if held is not None:
        held.expires_at = utcnow() + timedelta(seconds=lease_seconds)
        session.add(held)
        session.flush()
        return held

    row = Claim(
        change_id=change_id,
        participant_id=participant.id,  # type: ignore[arg-type]
        expires_at=utcnow() + timedelta(seconds=lease_seconds),
    )
    session.add(row)
    session.flush()
    return row


def release(session: Session, change_id: int, participant: Participant) -> bool:
    """Give up the lease. Returns False if the caller did not hold it."""
    held = active_claim(session, change_id)
    if held is None or held.participant_id != participant.id:
        return False
    held.status = ClaimStatus.RELEASED
    held.released_at = utcnow()
    session.add(held)
    session.flush()
    return True


def check_claim(session: Session, change_id: int, participant: Participant) -> None:
    """Raise if somebody else holds this change. An unclaimed change is fair game."""
    held = active_claim(session, change_id)
    if held is not None and held.participant_id != participant.id:
        other = session.get(Participant, held.participant_id)
        raise ClaimConflict(
            f"change {change_id} is being reviewed by "
            f"{other.name if other else 'another participant'}"
        )


# ------------------------------------------------------- agent disposition


@dataclass
class AgentRun:
    """What an agent pass did, and what it deliberately left alone."""

    agent: str
    policy_hash: str
    decided: int = 0
    left_to_humans: int = 0
    needs_confirmation: int = 0
    by_clause: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_clause is None:
            self.by_clause = {}


def agent_dispose(
    session: Session,
    run_id: int,
    agent: Participant,
    policy: Policy,
    *,
    dry_run: bool = False,
) -> AgentRun:
    """Let an agent decide every change the policy authorizes it to.

    Everything else is untouched and stays in the human queue. Each decision records the
    clause that permitted it, so the authority behind a machine decision is always
    specific and checkable.
    """
    if not agent.is_agent:
        raise WorkflowError(f"{agent.name} is not an agent")
    require_role(agent, Role.REVIEWER)

    from termguard.review import record_decision

    result = AgentRun(agent=agent.name, policy_hash=policy.hash)

    for change in _undecided_changes(session, run_id):
        hit = session.get(Hit, change.hit_id) if change.hit_id else None
        if hit is None:
            result.left_to_humans += 1
            continue

        clause = policy.authorizes(
            rule_id=hit.rule_id,
            classification=hit.classification.value,
            mechanism=change.mechanism.value,
            part=hit.part,
        )
        if clause is None:
            result.left_to_humans += 1
            continue

        result.decided += 1
        result.by_clause[clause.id] = result.by_clause.get(clause.id, 0) + 1
        if clause.requires_human_confirm:
            result.needs_confirmation += 1

        if dry_run:
            continue

        record_decision(
            session, change.id, DecisionKind(clause.decision), agent.name,  # type: ignore[arg-type]
            note=f"auto-decided under policy clause {clause.id}",
            participant=agent, clause=clause, policy_hash=policy.hash,
        )

    audit.record(
        session,
        "agent.disposed" if not dry_run else "agent.dry_run",
        summary=(
            f"{agent.name} decided {result.decided} change(s) under policy "
            f"{policy.hash}; {result.left_to_humans} left to humans"
        ),
        actor=agent.name, actor_kind=ActorKind.LLM, run_id=run_id,
        payload={
            "decided": result.decided,
            "left_to_humans": result.left_to_humans,
            "needs_confirmation": result.needs_confirmation,
            "by_clause": result.by_clause,
            "policy_hash": policy.hash,
        },
    )
    return result


def confirm_agent_batch(
    session: Session, run_id: int, confirmer: Participant, clause_id: str | None = None
) -> int:
    """A human confirming agent decisions that a policy clause said needed confirming."""
    if confirmer.is_agent:
        raise WorkflowError("an agent cannot confirm agent decisions")
    require_role(confirmer, Role.REVIEWER)

    statement = select(Decision).where(
        Decision.run_id == run_id,
        Decision.requires_human_confirm == True,  # noqa: E712 - SQL, not Python
        Decision.confirmed_by == None,  # noqa: E711
    )
    if clause_id:
        statement = statement.where(Decision.policy_clause_id == clause_id)

    rows = session.exec(statement).all()
    for row in rows:
        row.confirmed_by = confirmer.name
        row.confirmed_at = utcnow()
        session.add(row)
    session.flush()

    if rows:
        audit.record(
            session, "agent.batch_confirmed",
            summary=f"{confirmer.name} confirmed {len(rows)} agent decision(s)"
                    + (f" under clause {clause_id}" if clause_id else ""),
            actor=confirmer.name, actor_kind=ActorKind.HUMAN, run_id=run_id,
            payload={"count": len(rows), "clause_id": clause_id},
        )
    return len(rows)


# ------------------------------------------------------------------ sign-off


def separation_of_duties(
    session: Session, run_id: int, participant: Participant
) -> tuple[bool, list[str]]:
    """Whether this participant may sign off this run, and why not if they may not.

    Returns every blocker rather than the first, because being told one reason at a time
    is a bad experience for something that should be checked before the button is shown.
    """
    blockers: list[str] = []

    if participant.is_agent:
        blockers.append(
            "an agent cannot sign off a run; sign-off is a human accountability act"
        )
    if not participant.has_role(Role.APPROVER):
        blockers.append(
            f"{participant.name} does not hold the approver role "
            f"(has: {', '.join(participant.roles) or 'none'})"
        )

    own = session.exec(
        select(func.count()).select_from(Decision).where(
            Decision.run_id == run_id, Decision.reviewer == participant.name
        )
    ).one()
    if own:
        blockers.append(
            f"{participant.name} recorded {own} decision(s) in this run; under the "
            "maker-checker rule the approver must not also be a reviewer of it"
        )

    return (not blockers), blockers


def signoff_readiness(session: Session, run_id: int) -> dict[str, Any]:
    """Everything blocking sign-off, so the UI can show it before anyone tries."""
    undecided = len(_undecided_changes(session, run_id))
    unconfirmed = session.exec(
        select(func.count()).select_from(Decision).where(
            Decision.run_id == run_id,
            Decision.requires_human_confirm == True,  # noqa: E712
            Decision.confirmed_by == None,  # noqa: E711
        )
    ).one()

    run = session.get(Run, run_id)
    verified = run is not None and run.status.value == "verified"

    blockers: list[str] = []
    if undecided:
        blockers.append(f"{undecided} change(s) still undecided")
    if unconfirmed:
        blockers.append(f"{unconfirmed} agent decision(s) awaiting human confirmation")
    if not verified:
        blockers.append("the verification gate has not passed for this run")

    eligible = [
        p.name
        for p in session.exec(select(Participant).where(Participant.active == True)).all()  # noqa: E712
        if separation_of_duties(session, run_id, p)[0]
    ]

    return {
        "ready": not blockers,
        "blockers": blockers,
        "undecided": undecided,
        "unconfirmed_agent_decisions": unconfirmed,
        "verified": verified,
        "eligible_approvers": eligible,
        "signed": _signoff_summary(session, run_id),
    }


def _signoff_summary(session: Session, run_id: int) -> list[dict[str, Any]]:
    rows = session.exec(
        select(SignOff).where(SignOff.run_id == run_id).order_by(SignOff.id)
    ).all()
    out = []
    for row in rows:
        participant = session.get(Participant, row.participant_id)
        out.append(
            {
                "participant": participant.name if participant else "",
                "decision": row.decision.value,
                "signed_at": row.signed_at.isoformat(),
                "note": row.note,
                "rulebook_hash": row.rulebook_hash,
                "policy_hash": row.policy_hash,
            }
        )
    return out


def sign_off(
    session: Session,
    run_id: int,
    participant: Participant,
    decision: SignOffDecision | str,
    *,
    note: str | None = None,
    policy: Policy | None = None,
    force: bool = False,
) -> SignOff:
    """Approve or reject a run. Enforces separation of duties and readiness.

    ``force`` skips the *readiness* checks (undecided changes, verification) so a run can
    be explicitly rejected while it is still incomplete. It never skips separation of
    duties - that check has no override, because an override is what an auditor would ask
    about first.
    """
    kind = SignOffDecision(decision) if isinstance(decision, str) else decision

    permitted, blockers = separation_of_duties(session, run_id, participant)
    if not permitted:
        raise WorkflowError("; ".join(blockers))

    readiness = signoff_readiness(session, run_id)
    if kind is SignOffDecision.APPROVED and not readiness["ready"] and not force:
        raise WorkflowError(
            "run is not ready for approval: " + "; ".join(readiness["blockers"])
        )

    run = session.get(Run, run_id)
    if run is None:
        raise WorkflowError(f"no run {run_id}")

    counts = {
        "changes": session.exec(
            select(func.count()).select_from(Change).where(Change.run_id == run_id)
        ).one(),
        "decisions": session.exec(
            select(func.count()).select_from(Decision).where(Decision.run_id == run_id)
        ).one(),
        "agent_decisions": session.exec(
            select(func.count()).select_from(Decision).where(
                Decision.run_id == run_id, Decision.decided_by_kind == ActorKind.LLM
            )
        ).one(),
    }

    row = SignOff(
        run_id=run_id,
        participant_id=participant.id,  # type: ignore[arg-type]
        decision=kind,
        note=note,
        rulebook_hash=run.rulebook_hash,
        corpus_hash=run.corpus_hash,
        policy_hash=policy.hash if policy else "",
        covered=counts,
    )
    session.add(row)
    session.flush()

    audit.record(
        session, f"run.{kind.value}",
        summary=f"{participant.name} {kind.value} run {run_id} "
                f"({counts['decisions']} decisions, {counts['agent_decisions']} by agents)",
        actor=participant.name, actor_kind=ActorKind.HUMAN, run_id=run_id,
        payload={**counts, "note": note, "rulebook_hash": run.rulebook_hash,
                 "policy_hash": row.policy_hash, "forced": force},
    )
    session.commit()
    return row
