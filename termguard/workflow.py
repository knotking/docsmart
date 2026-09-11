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
    """The named person a change is assigned to.

    None for a change sitting in a team pool - that is not "unassigned", it is assigned to
    a group. Use :func:`current_assignment` when the distinction matters.
    """
    row = current_assignment(session, change_id)
    if row is None or row.participant_id is None:
        return None
    return session.get(Participant, row.participant_id)


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


# ------------------------------------------------------- routing and handoff
#
# Work reaches a team pool, a member claims it, and every subsequent movement is recorded
# as a Handoff rather than by mutating the change. That matters for the same reason the
# decision log is append-only: "who had this, and why did it move" is a question asked
# months later, and a mutable owner field cannot answer it.


def current_assignment(session: Session, change_id: int):
    """The newest assignment row for a change, or None."""
    from termguard.models import Assignment as AssignmentRow

    return session.exec(
        select(AssignmentRow)
        .where(AssignmentRow.change_id == change_id)
        .order_by(AssignmentRow.id.desc())  # type: ignore[union-attr]
    ).first()


def assign_to_team(
    session: Session,
    run_id: int,
    change_ids: Sequence[int],
    team,
    *,
    assigned_by: str = "system",
    reason: str | None = None,
    kind=None,
) -> int:
    """Route changes into a team's pool. Any active member may then claim one."""
    from termguard.models import Assignment as AssignmentRow
    from termguard.models import Handoff, HandoffKind

    kind = kind or HandoffKind.ROUTE
    for change_id in change_ids:
        previous = current_assignment(session, change_id)
        session.add(AssignmentRow(
            run_id=run_id, change_id=change_id, team_id=team.id,
            assigned_by=assigned_by, reason=reason,
        ))
        session.add(Handoff(
            run_id=run_id, change_id=change_id, kind=kind,
            from_participant_id=previous.participant_id if previous else None,
            from_team_id=previous.team_id if previous else None,
            to_team_id=team.id, actor=assigned_by, reason=reason,
        ))
    session.flush()
    audit.record(
        session, f"changes.{kind.value}",
        summary=f"{len(change_ids)} change(s) -> team {team.name}"
                + (f" ({reason})" if reason else ""),
        actor=assigned_by, run_id=run_id,
        payload={"team": team.slug, "count": len(change_ids), "reason": reason},
    )
    return len(change_ids)


def route_by_rule_owner(
    session: Session, run_id: int, rulebook, *, assigned_by: str = "system"
) -> dict[str, Any]:
    """Send every unassigned change to the team that owns its rule.

    The rulebook already records an owner per rule, so this needs no separate mapping.
    Rules whose owner has no team are reported rather than dropped into a default queue -
    silently defaulting is how work ends up somewhere nobody is watching.
    """
    from termguard import teams as teams_module
    from termguard.models import Hit

    missing = teams_module.unrouted_owners(session, rulebook)
    by_team: dict[str, list[int]] = {}
    unroutable: list[int] = []

    for change in _undecided_changes(session, run_id):
        assignment = current_assignment(session, change.id)
        if assignment is not None and assignment.pinned:
            continue  # a pinned owner is a deliberate choice; routing must not undo it
        hit = session.get(Hit, change.hit_id) if change.hit_id else None
        team = (
            teams_module.team_for_rule(session, rulebook, hit.rule_id) if hit else None
        )
        if team is None:
            unroutable.append(change.id)  # type: ignore[arg-type]
            continue
        by_team.setdefault(team.slug, []).append(change.id)  # type: ignore[arg-type]

    routed: dict[str, int] = {}
    for slug, change_ids in sorted(by_team.items()):
        team = teams_module.get_team(session, slug)
        routed[slug] = assign_to_team(
            session, run_id, change_ids, team,
            assigned_by=assigned_by, reason="rule owner",
        )

    return {
        "routed": routed,
        "total": sum(routed.values()),
        "unroutable": len(unroutable),
        "owners_without_a_team": missing,
    }


def _record_handoff(
    session: Session,
    run_id: int,
    change_id: int,
    kind,
    *,
    actor: str,
    reason: str,
    to_participant=None,
    to_team=None,
    resolves: int | None = None,
):
    from termguard.models import Assignment as AssignmentRow
    from termguard.models import Handoff

    previous = current_assignment(session, change_id)
    row = Handoff(
        run_id=run_id, change_id=change_id, kind=kind,
        from_participant_id=previous.participant_id if previous else None,
        from_team_id=previous.team_id if previous else None,
        to_participant_id=to_participant.id if to_participant else None,
        to_team_id=to_team.id if to_team else None,
        actor=actor, reason=reason, resolves_handoff_id=resolves,
    )
    session.add(row)

    if to_participant is not None or to_team is not None:
        session.add(AssignmentRow(
            run_id=run_id, change_id=change_id,
            participant_id=to_participant.id if to_participant else None,
            team_id=to_team.id if to_team else None,
            pinned=to_participant is not None,
            assigned_by=actor, reason=reason,
        ))
    session.flush()
    return row


def reassign(
    session: Session, run_id: int, change_id: int, *, actor: Participant,
    to_participant: Participant | None = None, to_team=None, reason: str,
):
    """Hand a change to another person or team, with a recorded reason."""
    if not reason.strip():
        raise WorkflowError("a reassignment must say why; work changing hands silently "
                            "is the thing this record exists to prevent")
    if to_participant is None and to_team is None:
        raise WorkflowError("reassign needs a destination participant or team")

    from termguard.models import HandoffKind

    # The lease belongs to whoever was working it; it does not travel with the change.
    held = active_claim(session, change_id)
    if held is not None:
        holder = session.get(Participant, held.participant_id)
        if holder is not None:
            release(session, change_id, holder)

    row = _record_handoff(
        session, run_id, change_id, HandoffKind.REASSIGN, actor=actor.name,
        reason=reason, to_participant=to_participant, to_team=to_team,
    )
    destination = to_participant.name if to_participant else to_team.name
    audit.record(
        session, "change.reassigned",
        summary=f"{actor.name} reassigned change {change_id} to {destination}: {reason}",
        actor=actor.name, actor_kind=ActorKind.HUMAN, run_id=run_id, change_id=change_id,
        payload={"to": destination, "reason": reason,
                 "pinned": to_participant is not None},
    )
    return row


def escalate(
    session: Session, run_id: int, change_id: int, *, actor: Participant, reason: str,
    to: Participant | None = None,
):
    """Send a change a reviewer cannot decide up to a team lead.

    The change stays open and *visibly* escalated. Leaving it undecided instead would be
    indistinguishable from nobody having looked at it yet, which is how hard cases sit
    untouched until the deadline.
    """
    if not reason.strip():
        raise WorkflowError("an escalation must say what the reviewer could not decide")

    from termguard import teams as teams_module
    from termguard.models import HandoffKind, Team

    target = to
    if target is None:
        assignment = current_assignment(session, change_id)
        team = (
            session.get(Team, assignment.team_id)
            if assignment and assignment.team_id else None
        )
        if team is None:
            for candidate in teams_module.teams_of(session, actor):
                if teams_module.leads(session, candidate):
                    team = candidate
                    break
        candidates = teams_module.leads(session, team) if team else []
        candidates = [p for p in candidates if p.id != actor.id]
        if not candidates:
            raise WorkflowError(
                "no team lead to escalate to; give the owning team a lead "
                "(teams.add_member(..., role='lead')) or name one explicitly"
            )
        target = candidates[0]

    row = _record_handoff(
        session, run_id, change_id, HandoffKind.ESCALATE, actor=actor.name,
        reason=reason, to_participant=target,
    )
    audit.record(
        session, "change.escalated",
        summary=f"{actor.name} escalated change {change_id} to {target.name}: {reason}",
        actor=actor.name, actor_kind=ActorKind.HUMAN, run_id=run_id, change_id=change_id,
        payload={"to": target.name, "reason": reason},
    )
    return row


def return_for_clarification(
    session: Session, run_id: int, change_id: int, *, actor: Participant, reason: str,
    to_team=None, to_participant: Participant | None = None,
):
    """Send a change back with a question. It leaves the review queue until answered."""
    if not reason.strip():
        raise WorkflowError("a return must carry the question being asked")
    if to_team is None and to_participant is None:
        raise WorkflowError("a return needs somewhere to go back to")

    from termguard.models import HandoffKind

    held = active_claim(session, change_id)
    if held is not None:
        holder = session.get(Participant, held.participant_id)
        if holder is not None:
            release(session, change_id, holder)

    row = _record_handoff(
        session, run_id, change_id, HandoffKind.RETURN, actor=actor.name, reason=reason,
        to_participant=to_participant, to_team=to_team,
    )
    destination = to_participant.name if to_participant else to_team.name
    audit.record(
        session, "change.returned",
        summary=f"{actor.name} returned change {change_id} to {destination}: {reason}",
        actor=actor.name, actor_kind=ActorKind.HUMAN, run_id=run_id, change_id=change_id,
        payload={"to": destination, "question": reason},
    )
    return row


def resolve_return(
    session: Session, run_id: int, change_id: int, *, actor: Participant, answer: str,
):
    """Answer an outstanding return so the change re-enters the review queue."""
    if not answer.strip():
        raise WorkflowError("resolving a return requires the answer")

    from termguard.models import HandoffKind, Team

    outstanding = open_return(session, change_id)
    if outstanding is None:
        raise WorkflowError(f"change {change_id} has no outstanding return")

    row = _record_handoff(
        session, run_id, change_id, HandoffKind.RESOLVE, actor=actor.name, reason=answer,
        to_participant=(
            session.get(Participant, outstanding.from_participant_id)
            if outstanding.from_participant_id else None
        ),
        to_team=(
            session.get(Team, outstanding.from_team_id)
            if outstanding.from_team_id else None
        ),
        resolves=outstanding.id,
    )
    audit.record(
        session, "change.return_resolved",
        summary=f"{actor.name} answered the return on change {change_id}",
        actor=actor.name, actor_kind=ActorKind.HUMAN, run_id=run_id, change_id=change_id,
        payload={"answer": answer, "resolves_handoff_id": outstanding.id},
    )
    return row


def handoffs(session: Session, change_id: int):
    """Every movement of a change, oldest first - its routing history."""
    from termguard.models import Handoff

    return list(session.exec(
        select(Handoff).where(Handoff.change_id == change_id).order_by(Handoff.id)
    ).all())


def open_return(session: Session, change_id: int):
    """The unanswered return on a change, if there is one."""
    from termguard.models import HandoffKind

    history = handoffs(session, change_id)
    answered = {h.resolves_handoff_id for h in history if h.resolves_handoff_id}
    for row in reversed(history):
        if row.kind is HandoffKind.RETURN and row.id not in answered:
            return row
    return None


def change_state(session: Session, change_id: int) -> str:
    """Where a change sits: decided | returned | escalated | assigned | pooled | unassigned.

    Derived from the handoff history rather than stored, so it cannot drift from the
    events that produced it.
    """
    from termguard.models import Decision, HandoffKind

    decided = session.exec(
        select(Decision).where(Decision.change_id == change_id)
    ).first()
    if decided is not None:
        return "decided"
    if open_return(session, change_id) is not None:
        return "returned"

    history = handoffs(session, change_id)
    if history and history[-1].kind is HandoffKind.ESCALATE:
        return "escalated"

    assignment = current_assignment(session, change_id)
    if assignment is None:
        return "unassigned"
    return "assigned" if assignment.participant_id else "pooled"


def queue_for(session: Session, run_id: int, participant: Participant) -> list[int]:
    """The change ids this participant should be looking at.

    Their own assignments, their teams' pools, and anything they are covering for someone
    who is away. Returned changes are excluded - they are waiting on someone else.
    """
    from termguard import teams as teams_module
    from termguard.models import Delegation

    # Every participant whose work currently lands on me, following chains: if Alice is
    # covered by Bob and Bob is covered by Zoe, Alice's queue is Zoe's problem. Looking
    # only at delegations addressed directly to me would drop Alice on the floor while
    # every individual hop still looked correct.
    covering = {participant.id}
    delegated_ids = {
        row.participant_id for row in session.exec(select(Delegation)).all()
    }
    for covered_id in delegated_ids:
        covered = session.get(Participant, covered_id)
        if covered is None:
            continue
        if teams_module.effective_owner(session, covered).id == participant.id:
            covering.add(covered_id)

    team_ids = {team.id for team in teams_module.teams_of(session, participant)}

    out: list[int] = []
    for change in _undecided_changes(session, run_id):
        if change_state(session, change.id) == "returned":
            continue
        assignment = current_assignment(session, change.id)
        if assignment is None:
            continue
        if assignment.participant_id in covering:
            out.append(change.id)  # type: ignore[arg-type]
        elif assignment.participant_id is None and assignment.team_id in team_ids:
            out.append(change.id)  # type: ignore[arg-type]
    return out
