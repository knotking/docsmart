"""Layer D: the numbers worth watching.

Deliberately not "everything we can count". Four questions, and only metrics that answer
one of them:

**Posture** - where does the corpus stand right now? What a manager opens the tool to see.

**Trust in the AI** - the one that decides whether this stays switched on. The honest
measure is not how confident the model was, it is *what reviewers did with its proposals*:
a rule whose AI proposals get rejected 40% of the time is a rule whose context note is
wrong, and that is visible here long before it becomes an incident.

**Throughput** - who is doing the work, how fast, and how much never reaches a human at
all. This is what sizes a rollout.

**Rule health** - which rules fire, and which get overturned. A high rejection rate on a
rule is a defect in the rulebook, not in the reviewers.

Every metric is computed from rows, so any figure can be traced back to the decisions
behind it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlmodel import Session, func, select

from termguard.models import (
    ActorKind,
    Change,
    ChangeStatus,
    Classification,
    Decision,
    DecisionKind,
    Document,
    DocumentVersion,
    Hit,
    Mechanism,
    Participant,
    Run,
    RunStatus,
    SignOff,
    Stage,
)


def _rate(numerator: int, denominator: int) -> float | None:
    """A proportion, or None when there is nothing to divide by.

    None rather than 0.0 on purpose: "no data" and "zero percent" mean very different
    things, and a dashboard that renders them identically will mislead someone.
    """
    return round(numerator / denominator, 4) if denominator else None


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- posture


def posture(session: Session) -> dict[str, Any]:
    """Where the corpus stands. The landing view."""
    documents = session.exec(select(Document)).all()
    versions = session.exec(select(DocumentVersion)).all()

    latest_stage: dict[int, Stage] = {}
    for version in sorted(versions, key=lambda v: v.version_no):
        latest_stage[version.document_id] = version.stage

    stages = {stage.value: 0 for stage in Stage}
    for stage in latest_stage.values():
        stages[stage.value] += 1

    verified = stages.get(Stage.VERIFIED.value, 0)
    runs = session.exec(select(Run).order_by(Run.id.desc())).all()  # type: ignore[union-attr]

    total_changes = session.exec(select(func.count()).select_from(Change)).one()
    decided = session.exec(select(func.count()).select_from(Decision)).one()
    distinct_decided = len({d.change_id for d in session.exec(select(Decision)).all()})

    return {
        "documents": len(documents),
        "versions": len(versions),
        "bytes_stored": sum(v.size_bytes for v in versions),
        "documents_by_stage": stages,
        "verified_share": _rate(verified, len(documents)),
        "runs": {
            "total": len(runs),
            "by_status": {
                status.value: sum(1 for r in runs if r.status is status) for status in RunStatus
            },
            "latest_id": runs[0].id if runs else None,
        },
        "changes": {
            "total": total_changes,
            "decided": distinct_decided,
            "pending": total_changes - distinct_decided,
            "decision_rows": decided,
        },
        "signed_off_runs": session.exec(select(func.count()).select_from(SignOff)).one(),
    }


# ------------------------------------------------------------- AI trust


def ai_trust(session: Session, run_id: int | None = None) -> dict[str, Any]:
    """What reviewers actually did with the machine's proposals.

    Two distinct populations, and conflating them is the usual mistake:

    * **AI-proposed changes** - the model wrote an edit and a human ruled on it. The
      acceptance rate here measures the quality of the model's *suggestions*.
    * **Agent-decided changes** - an agent recorded the decision itself under policy.
      These have no reviewer verdict to measure, so they are reported separately as
      delegated volume, never folded into an accuracy number.
    """
    changes = _changes(session, run_id)
    latest = _latest_decisions(session, run_id)

    ai_changes = [c for c in changes if c.mechanism is Mechanism.AI]
    verdicts = {DecisionKind.ACCEPTED: 0, DecisionKind.REJECTED: 0, DecisionKind.EDITED: 0}
    by_rule: dict[str, dict[str, int]] = {}

    hits = {h.id: h for h in session.exec(select(Hit)).all()}

    for change in ai_changes:
        decision = latest.get(change.id)
        if decision is None or decision.decided_by_kind is ActorKind.LLM:
            continue  # undecided, or the agent ruled on itself - not a human verdict
        verdicts[decision.decision] += 1
        hit = hits.get(change.hit_id) if change.hit_id else None
        rule_id = hit.rule_id if hit else "unknown"
        bucket = by_rule.setdefault(rule_id, {"accepted": 0, "rejected": 0, "edited": 0})
        bucket[decision.decision.value] += 1

    judged = sum(verdicts.values())
    upheld = verdicts[DecisionKind.ACCEPTED]

    for rule_id, bucket in by_rule.items():
        total = sum(bucket.values())
        bucket["judged"] = total
        bucket["acceptance_rate"] = _rate(bucket["accepted"], total)  # type: ignore[assignment]

    # Escalations are the containment firing: the model's answer was refused by code.
    escalated = sum(1 for c in ai_changes if c.llm_decision == "escalate")
    kept = sum(1 for c in ai_changes if c.llm_decision == "keep")

    agent_decided = [d for d in latest.values() if d.decided_by_kind is ActorKind.LLM]
    by_clause: dict[str, int] = {}
    for decision in agent_decided:
        key = decision.policy_clause_id or "unattributed"
        by_clause[key] = by_clause.get(key, 0) + 1

    return {
        "ai_proposals": len(ai_changes),
        "judged_by_humans": judged,
        "accepted": verdicts[DecisionKind.ACCEPTED],
        "rejected": verdicts[DecisionKind.REJECTED],
        "edited": verdicts[DecisionKind.EDITED],
        "acceptance_rate": _rate(upheld, judged),
        "override_rate": _rate(verdicts[DecisionKind.REJECTED] + verdicts[DecisionKind.EDITED], judged),
        "model_said_keep": kept,
        "escalated_by_validation": escalated,
        "by_rule": dict(sorted(by_rule.items())),
        "agent_decided": {
            "count": len(agent_decided),
            "by_clause": dict(sorted(by_clause.items())),
            "unattributed": by_clause.get("unattributed", 0),
            "awaiting_confirmation": sum(
                1 for d in agent_decided if d.requires_human_confirm and not d.confirmed_by
            ),
        },
    }


# ------------------------------------------------------------ throughput


def throughput(session: Session, run_id: int | None = None) -> dict[str, Any]:
    """Who is doing the work, how fast, and how much never reaches a human."""
    changes = _changes(session, run_id)
    latest = _latest_decisions(session, run_id)
    participants = {p.id: p for p in session.exec(select(Participant)).all()}

    by_participant: dict[str, dict[str, Any]] = {}
    latencies: list[float] = []

    for change in changes:
        decision = latest.get(change.id)
        if decision is None:
            continue
        name = decision.reviewer
        entry = by_participant.setdefault(
            name,
            {
                "decisions": 0, "accepted": 0, "rejected": 0, "edited": 0,
                "kind": (
                    participants[decision.participant_id].kind.value
                    if decision.participant_id and decision.participant_id in participants
                    else ("agent" if decision.decided_by_kind is ActorKind.LLM else "human")
                ),
            },
        )
        entry["decisions"] += 1
        entry[decision.decision.value] += 1

        # Time from the change being written to a decision landing on it.
        if change.created_at and decision.decided_at:
            delta = (_aware(decision.decided_at) - _aware(change.created_at)).total_seconds()
            if 0 <= delta < 86_400 * 30:  # ignore clock skew and demo resets
                latencies.append(delta)

    deterministic = sum(1 for c in changes if c.mechanism is Mechanism.DETERMINISTIC)
    agent_decided = sum(1 for d in latest.values() if d.decided_by_kind is ActorKind.LLM)
    human_decided = sum(1 for d in latest.values() if d.decided_by_kind is not ActorKind.LLM)

    return {
        "changes": len(changes),
        "decided": len(latest),
        "human_decisions": human_decided,
        "agent_decisions": agent_decided,
        "automation_rate": _rate(agent_decided, len(latest)),
        "deterministic_share": _rate(deterministic, len(changes)),
        "by_participant": dict(
            sorted(by_participant.items(), key=lambda kv: -kv[1]["decisions"])
        ),
        "median_seconds_to_decision": (
            round(statistics.median(latencies), 1) if latencies else None
        ),
        "decisions_per_hour_estimate": _decisions_per_hour(session, run_id),
    }


# A review pace extrapolated from a very short span is not a measurement. A scripted run
# decides hundreds of changes in under a second, which annualizes to a number that is both
# absurd and, on a dashboard, quietly believable. Below these thresholds we report nothing.
MIN_PACE_DECISIONS = 10
MIN_PACE_SPAN_SECONDS = 120


def _decisions_per_hour(session: Session, run_id: int | None) -> float | None:
    """Observed human review pace, for sizing a rollout.

    Measured from human decisions only, over the span they actually occupied. Returns None
    unless there is enough of a span to be a real observation - a rate derived from a
    batch script is worse than no rate, because someone will plan against it.
    """
    statement = select(Decision).where(Decision.decided_by_kind != ActorKind.LLM)
    if run_id is not None:
        statement = statement.where(Decision.run_id == run_id)
    rows = sorted(session.exec(statement).all(), key=lambda d: d.decided_at)
    if len(rows) < MIN_PACE_DECISIONS:
        return None
    span = (_aware(rows[-1].decided_at) - _aware(rows[0].decided_at)).total_seconds()
    if span < MIN_PACE_SPAN_SECONDS:
        return None
    return round(len(rows) / (span / 3600), 1)


# ----------------------------------------------------------- rule health


def rule_health(session: Session, run_id: int | None = None) -> list[dict[str, Any]]:
    """Per-rule volume and how often reviewers overturned it.

    A rule with a high override rate is a defect in the rulebook - usually an approved
    term that is wrong in some context the rule does not describe. This is the table that
    tells the terminology owner what to fix.
    """
    hit_statement = select(Hit)
    if run_id is not None:
        hit_statement = hit_statement.where(Hit.run_id == run_id)
    hits = session.exec(hit_statement).all()
    hits_by_id = {h.id: h for h in hits}

    latest = _latest_decisions(session, run_id)
    changes = _changes(session, run_id)

    rules: dict[str, dict[str, Any]] = {}
    for hit in hits:
        entry = rules.setdefault(
            hit.rule_id,
            {"rule_id": hit.rule_id, "hits": 0, "unambiguous": 0, "needs_judgment": 0,
             "changes": 0, "accepted": 0, "rejected": 0, "edited": 0, "undecided": 0,
             "parts": {}},
        )
        entry["hits"] += 1
        entry[hit.classification.value] += 1
        entry["parts"][hit.part] = entry["parts"].get(hit.part, 0) + 1

    for change in changes:
        hit = hits_by_id.get(change.hit_id) if change.hit_id else None
        if hit is None:
            continue
        entry = rules.get(hit.rule_id)
        if entry is None:
            continue
        entry["changes"] += 1
        decision = latest.get(change.id)
        if decision is None:
            entry["undecided"] += 1
        else:
            entry[decision.decision.value] += 1

    out: list[dict[str, Any]] = []
    for entry in rules.values():
        judged = entry["accepted"] + entry["rejected"] + entry["edited"]
        entry["judged"] = judged
        entry["override_rate"] = _rate(entry["rejected"] + entry["edited"], judged)
        entry["outside_body"] = sum(
            count for part, count in entry["parts"].items() if part != "body"
        )
        out.append(entry)
    return sorted(out, key=lambda e: (-(e["override_rate"] or 0), -e["hits"]))


# ------------------------------------------------------------- assembly


def dashboard(session: Session, run_id: int | None = None) -> dict[str, Any]:
    """Everything the metrics screen needs, in one round trip."""
    if run_id is None:
        latest_run = session.exec(select(Run).order_by(Run.id.desc())).first()  # type: ignore[union-attr]
        run_id = latest_run.id if latest_run else None

    health = rule_health(session, run_id)
    trust = ai_trust(session, run_id)

    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "posture": posture(session),
        "ai_trust": trust,
        "throughput": throughput(session, run_id),
        "rule_health": health,
        "attention": _attention(session, run_id, trust, health),
    }


def _attention(
    session: Session, run_id: int | None, trust: dict[str, Any], health: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """The short list of things actually worth acting on.

    A dashboard nobody acts on is decoration. These are the conditions that should change
    somebody's afternoon, phrased as what to do about them.
    """
    items: list[dict[str, str]] = []

    pending = posture(session)["changes"]["pending"]
    if pending:
        items.append({
            "severity": "info",
            "title": f"{pending} change(s) awaiting a decision",
            "detail": "The verification gate will not pass until every change is decided.",
        })

    awaiting = trust["agent_decided"]["awaiting_confirmation"]
    if awaiting:
        items.append({
            "severity": "warn",
            "title": f"{awaiting} agent decision(s) awaiting human confirmation",
            "detail": "A policy clause required a person to confirm these before sign-off.",
        })

    if trust["agent_decided"]["unattributed"]:
        items.append({
            "severity": "error",
            "title": f"{trust['agent_decided']['unattributed']} agent decision(s) with no policy clause",
            "detail": "Every machine decision must name the authority behind it. Investigate.",
        })

    if trust["escalated_by_validation"]:
        items.append({
            "severity": "info",
            "title": f"{trust['escalated_by_validation']} model response(s) rejected by validation",
            "detail": "The containment fired and sent these to a human. Working as designed.",
        })

    for rule in health:
        rate = rule["override_rate"]
        if rate is not None and rate >= 0.25 and rule["judged"] >= 4:
            items.append({
                "severity": "warn",
                "title": f"{rule['rule_id']}: reviewers overturned {rate:.0%} of proposals",
                "detail": "A high override rate usually means the rule's approved term or "
                          "context note is wrong, not that the reviewers are.",
            })

    unverified = posture(session)["documents_by_stage"].get(Stage.REDLINED.value, 0)
    if unverified:
        items.append({
            "severity": "info",
            "title": f"{unverified} document(s) redlined but not yet verified",
            "detail": "Run the verification gate once the queue is clear.",
        })

    return items


# -------------------------------------------------------------- helpers


def _changes(session: Session, run_id: int | None) -> list[Change]:
    statement = select(Change)
    if run_id is not None:
        statement = statement.where(Change.run_id == run_id)
    return list(session.exec(statement.order_by(Change.id)).all())


def _latest_decisions(session: Session, run_id: int | None) -> dict[int, Decision]:
    statement = select(Decision)
    if run_id is not None:
        statement = statement.where(Decision.run_id == run_id)
    latest: dict[int, Decision] = {}
    for row in session.exec(statement.order_by(Decision.id)).all():
        latest[row.change_id] = row  # later rows supersede
    return latest
