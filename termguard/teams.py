"""Layer D: organizations, teams, membership and cover.

Structure, not tenancy. Nothing here scopes a query - the CLAUDE.md "no multi-tenancy"
constraint stands and IAM in front of the service remains the access boundary. What this
buys is *routing* and *accountability*: work reaches the group that owns the rule, and
every person acting on a change resolves to a team.

The join to the rulebook is deliberate. Every rule already carries an ``owner``
(``regulatory-affairs``, ``clinical-affairs``, ...), so a team's ``slug`` is that owner
string. No second mapping to maintain, and no way for the two to drift: if a rule names an
owner with no team, :func:`unrouted_owners` says so rather than silently dropping the work
into a default queue.

**Delegation does not move assignments.** It changes who the *effective* owner is while it
is in force, and lapses on its own. Reassigning a hundred changes because somebody took a
week off - and then reassigning them back - is how work gets lost.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlmodel import Session, select

from termguard import audit
from termguard.models import (
    ActorKind,
    Delegation,
    Membership,
    Organization,
    Participant,
    Team,
    TeamRole,
    utcnow,
)
from termguard.rulebook import Rulebook

DEFAULT_ORG_SLUG = "default"


class TeamError(RuntimeError):
    """A team rule was violated. The message is written to be shown to the user."""


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------ organizations


def ensure_org(session: Session, name: str, slug: str | None = None) -> Organization:
    """Find or create an organization."""
    slug = slug or name.lower().replace(" ", "-")
    existing = session.exec(select(Organization).where(Organization.slug == slug)).first()
    if existing is not None:
        return existing
    org = Organization(name=name, slug=slug)
    session.add(org)
    session.flush()
    audit.record(session, "org.created", summary=f"organization {name} created",
                 actor="system", payload={"slug": slug})
    return org


def default_org(session: Session) -> Organization:
    """The single organization a single-company deployment needs."""
    return ensure_org(session, "Default organization", DEFAULT_ORG_SLUG)


# -------------------------------------------------------------------- teams


def ensure_team(
    session: Session,
    slug: str,
    *,
    name: str | None = None,
    org: Organization | None = None,
    description: str | None = None,
) -> Team:
    """Find or create a team within an organization."""
    org = org or default_org(session)
    # Looked up by slug alone, matching the uniqueness constraint and the way routing
    # resolves a team. Re-seeding from a different org returns the existing team rather
    # than creating a shadow of it.
    existing = session.exec(select(Team).where(Team.slug == slug)).first()
    if existing is not None:
        return existing

    team = Team(
        org_id=org.id,  # type: ignore[arg-type]
        slug=slug,
        name=name or slug.replace("-", " ").title(),
        description=description,
    )
    session.add(team)
    session.flush()
    audit.record(session, "team.created", summary=f"team {team.name} created",
                 actor="system", payload={"slug": slug, "org": org.slug})
    return team


def get_team(session: Session, identifier: int | str) -> Team:
    team = (
        session.get(Team, identifier)
        if isinstance(identifier, int)
        else session.exec(select(Team).where(Team.slug == identifier)).first()
    )
    if team is None:
        raise TeamError(f"unknown team {identifier!r}")
    return team


def teams_from_rulebook(session: Session, rulebook: Rulebook,
                        org: Organization | None = None) -> dict[str, Team]:
    """Create one team per distinct rule owner.

    The rulebook is the source of truth for who owns what, so the team list is derived
    from it rather than maintained alongside it.
    """
    org = org or default_org(session)
    owners = sorted({rule.owner for rule in rulebook if rule.owner})
    return {
        owner: ensure_team(
            session, owner, org=org,
            description=f"Owns {', '.join(r.id for r in rulebook if r.owner == owner)}",
        )
        for owner in owners
    }


def team_for_rule(session: Session, rulebook: Rulebook, rule_id: str) -> Team | None:
    """The team that owns a rule, or None if the rule names no owner."""
    try:
        owner = rulebook.get(rule_id).owner
    except KeyError:
        return None
    if not owner:
        return None
    return session.exec(select(Team).where(Team.slug == owner)).first()


def unrouted_owners(session: Session, rulebook: Rulebook) -> dict[str, list[str]]:
    """Rule owners with no corresponding team, and the rules that would go nowhere.

    Called before routing so missing teams surface as a listed problem rather than as
    work quietly landing in a default queue nobody watches.
    """
    known = {team.slug for team in session.exec(select(Team)).all()}
    missing: dict[str, list[str]] = {}
    for rule in rulebook:
        owner = rule.owner or "(unowned)"
        if owner not in known:
            missing.setdefault(owner, []).append(rule.id)
    return missing


# --------------------------------------------------------------- membership


def add_member(
    session: Session,
    team: Team,
    participant: Participant,
    *,
    role: TeamRole | str = TeamRole.MEMBER,
) -> Membership:
    """Put a participant on a team, or update the role they hold on it."""
    role = TeamRole(role) if isinstance(role, str) else role
    existing = session.exec(
        select(Membership).where(
            Membership.team_id == team.id,
            Membership.participant_id == participant.id,
            Membership.left_at == None,  # noqa: E711
        )
    ).first()
    if existing is not None:
        if existing.team_role is not role:
            existing.team_role = role
            session.add(existing)
            session.flush()
            audit.record(
                session, "team.role_changed",
                summary=f"{participant.name} is now {role.value} of {team.name}",
                actor="system", payload={"team": team.slug, "role": role.value},
            )
        return existing

    row = Membership(
        team_id=team.id,  # type: ignore[arg-type]
        participant_id=participant.id,  # type: ignore[arg-type]
        team_role=role,
    )
    session.add(row)
    session.flush()
    audit.record(
        session, "team.member_added",
        summary=f"{participant.name} joined {team.name} as {role.value}",
        actor="system",
        actor_kind=ActorKind.LLM if participant.is_agent else ActorKind.HUMAN,
        payload={"team": team.slug, "role": role.value, "kind": participant.kind.value},
    )
    return row


def remove_member(session: Session, team: Team, participant: Participant) -> bool:
    """Take a participant off a team. Records a leaving date; never deletes the row."""
    row = session.exec(
        select(Membership).where(
            Membership.team_id == team.id,
            Membership.participant_id == participant.id,
            Membership.left_at == None,  # noqa: E711
        )
    ).first()
    if row is None:
        return False
    row.left_at = utcnow()
    session.add(row)
    session.flush()
    audit.record(session, "team.member_removed",
                 summary=f"{participant.name} left {team.name}", actor="system",
                 payload={"team": team.slug})
    return True


def members(session: Session, team: Team, *, role: TeamRole | None = None) -> list[Participant]:
    """Current members of a team, optionally filtered by the role they hold on it."""
    statement = select(Membership).where(
        Membership.team_id == team.id, Membership.left_at == None  # noqa: E711
    )
    if role is not None:
        statement = statement.where(Membership.team_role == role)
    rows = session.exec(statement.order_by(Membership.id)).all()
    found = [session.get(Participant, row.participant_id) for row in rows]
    return [p for p in found if p is not None and p.active]


def leads(session: Session, team: Team) -> list[Participant]:
    """A team's leads - the escalation target for its work."""
    return members(session, team, role=TeamRole.LEAD)


def teams_of(session: Session, participant: Participant) -> list[Team]:
    rows = session.exec(
        select(Membership).where(
            Membership.participant_id == participant.id,
            Membership.left_at == None,  # noqa: E711
        )
    ).all()
    found = [session.get(Team, row.team_id) for row in rows]
    return [t for t in found if t is not None]


def is_member(session: Session, team: Team, participant: Participant) -> bool:
    return any(t.id == team.id for t in teams_of(session, participant))


# --------------------------------------------------------------- delegation


def delegate(
    session: Session,
    participant: Participant,
    to: Participant,
    *,
    until: datetime | None = None,
    reason: str | None = None,
    created_by: str = "system",
) -> Delegation:
    """Hand a participant's queue to someone else while they are away.

    Refuses an agent as the delegate: cover for an absent human must not silently become
    machine authority over work a person was meant to see.
    """
    if participant.id == to.id:
        raise TeamError("a participant cannot delegate to themselves")
    if to.is_agent:
        raise TeamError(
            "cover must be a human; delegating a person's queue to an agent would turn "
            "an absence into machine authority"
        )
    if not to.active:
        raise TeamError(f"{to.name} is not active")

    row = Delegation(
        participant_id=participant.id,  # type: ignore[arg-type]
        delegate_id=to.id,  # type: ignore[arg-type]
        ends_at=until,
        reason=reason,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    audit.record(
        session, "delegation.created",
        summary=f"{participant.name}'s queue is covered by {to.name}"
                + (f" until {until.isoformat()}" if until else ""),
        actor=created_by, actor_kind=ActorKind.HUMAN,
        payload={"from": participant.name, "to": to.name, "reason": reason,
                 "ends_at": until.isoformat() if until else None},
    )
    return row


def revoke_delegation(session: Session, participant: Participant, *, by: str = "system") -> int:
    """End any cover currently in force for a participant."""
    now = utcnow()
    active = [d for d in _delegations_for(session, participant) if _in_force(d, now)]
    for row in active:
        row.revoked_at = now
        session.add(row)
    session.flush()
    if active:
        audit.record(session, "delegation.revoked",
                     summary=f"cover for {participant.name} ended", actor=by,
                     payload={"count": len(active)})
    return len(active)


def _delegations_for(session: Session, participant: Participant) -> list[Delegation]:
    return list(
        session.exec(
            select(Delegation)
            .where(Delegation.participant_id == participant.id)
            .order_by(Delegation.id.desc())  # type: ignore[union-attr]
        ).all()
    )


def _in_force(row: Delegation, now: datetime) -> bool:
    if row.revoked_at is not None:
        return False
    starts = _aware(row.starts_at)
    ends = _aware(row.ends_at)
    if starts is not None and starts > now:
        return False
    return ends is None or ends > now


def active_delegation(session: Session, participant: Participant) -> Delegation | None:
    """The cover currently standing for a participant, if any."""
    now = utcnow()
    for row in _delegations_for(session, participant):
        if _in_force(row, now):
            return row
    return None


def effective_owner(session: Session, participant: Participant, *, depth: int = 0) -> Participant:
    """Who is actually covering this participant's work right now.

    Follows a chain of delegations - A covers for B who is covering for C - but stops at
    a short depth and returns the last participant reached. A cycle here would hang the
    queue, and a hung queue is worse than cover landing one hop short.
    """
    if depth >= 4:
        return participant
    row = active_delegation(session, participant)
    if row is None:
        return participant
    delegate_participant = session.get(Participant, row.delegate_id)
    if delegate_participant is None or delegate_participant.id == participant.id:
        return participant
    return effective_owner(session, delegate_participant, depth=depth + 1)


def describe(session: Session, participant: Participant) -> dict[str, Any]:
    """A participant with their teams and any cover in force - one payload for the UI."""
    cover = active_delegation(session, participant)
    covering_for = [
        session.get(Participant, row.participant_id)
        for row in session.exec(
            select(Delegation).where(Delegation.delegate_id == participant.id)
        ).all()
        if _in_force(row, utcnow())
    ]
    return {
        "participant_id": participant.id,
        "name": participant.name,
        "kind": participant.kind.value,
        "roles": participant.roles,
        "active": participant.active,
        "teams": [
            {"slug": team.slug, "name": team.name,
             "lead": any(p.id == participant.id for p in leads(session, team))}
            for team in teams_of(session, participant)
        ],
        "away": cover is not None,
        "covered_by": (
            session.get(Participant, cover.delegate_id).name if cover else None
        ),
        "cover_until": cover.ends_at.isoformat() if cover and cover.ends_at else None,
        "covering_for": [p.name for p in covering_for if p is not None],
    }
