"""Layer D: several participants on one run.

The interesting assertions are the refusals — a second reviewer blocked from a claimed
change, an agent blocked from signing, a reviewer blocked from approving their own work.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from termguard import metrics, review, verify, workflow
from termguard.models import (
    ActorKind,
    Change,
    ClaimStatus,
    Decision,
    DecisionKind,
    ParticipantKind,
    Role,
    SignOff,
    SignOffDecision,
    utcnow,
)
from termguard.pipeline import run_pipeline
from termguard.policy import load_policy
from termguard.workflow import ClaimConflict, WorkflowError

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBSET = ("IFU-001.docx", "RMS-001.docx")


@pytest.fixture(scope="module")
def policy():
    return load_policy(REPO_ROOT / "data" / "policy.yaml")


@pytest.fixture
def wf_settings(settings, corpus_dir: Path, tmp_path: Path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in SUBSET:
        shutil.copy(corpus_dir / name, corpus / name)
    return replace(settings, corpus_dir=corpus)


@pytest.fixture
def team(session):
    return {
        "alice": workflow.ensure_participant(session, "alice", roles=[Role.REVIEWER]),
        "bob": workflow.ensure_participant(session, "bob", roles=[Role.REVIEWER]),
        "dana": workflow.ensure_participant(session, "dana", roles=[Role.APPROVER]),
        "agent": workflow.ensure_participant(
            session, "agent-1", kind=ParticipantKind.AGENT, roles=[Role.REVIEWER],
            model="claude-opus-5",
        ),
    }


@pytest.fixture
def run(session, store, wf_settings):
    result, _ = run_pipeline(session, settings=wf_settings, store=store, dry_run=True)
    return result


class TestParticipants:
    def test_registration_records_kind_and_roles(self, session) -> None:
        agent = workflow.ensure_participant(
            session, "a1", kind=ParticipantKind.AGENT, roles=[Role.REVIEWER], model="m"
        )
        assert agent.is_agent and agent.has_role(Role.REVIEWER) and agent.model == "m"

    def test_registration_is_idempotent(self, session) -> None:
        first = workflow.ensure_participant(session, "x", roles=[Role.REVIEWER])
        second = workflow.ensure_participant(session, "x", roles=[Role.REVIEWER])
        assert first.id == second.id

    def test_roles_widen_but_never_narrow(self, session) -> None:
        """Revoking a role is an administrative act, not something a routine call does."""
        workflow.ensure_participant(session, "y", roles=[Role.REVIEWER])
        widened = workflow.ensure_participant(session, "y", roles=[Role.APPROVER])
        assert set(widened.roles) == {"reviewer", "approver"}

    def test_unknown_participant_raises(self, session) -> None:
        with pytest.raises(WorkflowError, match="unknown participant"):
            workflow.get_participant(session, "nobody")

    def test_a_participant_without_the_role_is_refused(self, session) -> None:
        observer = workflow.ensure_participant(session, "obs", roles=[Role.OBSERVER])
        with pytest.raises(WorkflowError, match="does not hold the reviewer role"):
            workflow.claim(session, 1, observer)


class TestClaims:
    def test_a_claim_blocks_another_participant(self, session, run, team) -> None:
        change = review.pending_changes(session, run.id)[0]
        workflow.claim(session, change.id, team["alice"])
        with pytest.raises(ClaimConflict, match="alice"):
            workflow.claim(session, change.id, team["bob"])

    def test_a_claim_blocks_the_other_participants_decision(self, session, run, team) -> None:
        """The collision this exists to prevent: a silent superseding decision."""
        change = review.pending_changes(session, run.id)[0]
        workflow.claim(session, change.id, team["alice"])
        with pytest.raises(ClaimConflict):
            review.record_decision(session, change.id, "accepted", "bob",
                                   participant=team["bob"])

    def test_the_holder_can_decide(self, session, run, team) -> None:
        change = review.pending_changes(session, run.id)[0]
        workflow.claim(session, change.id, team["alice"])
        decision = review.record_decision(session, change.id, "accepted", "alice",
                                          participant=team["alice"])
        assert decision.participant_id == team["alice"].id

    def test_releasing_frees_the_change(self, session, run, team) -> None:
        change = review.pending_changes(session, run.id)[0]
        workflow.claim(session, change.id, team["alice"])
        assert workflow.release(session, change.id, team["alice"]) is True
        workflow.claim(session, change.id, team["bob"])  # no longer blocked

    def test_releasing_someone_elses_claim_fails(self, session, run, team) -> None:
        change = review.pending_changes(session, run.id)[0]
        workflow.claim(session, change.id, team["alice"])
        assert workflow.release(session, change.id, team["bob"]) is False

    def test_an_expired_lease_frees_the_change(self, session, run, team) -> None:
        """A reviewer who closes their laptop must not block the queue forever."""
        change = review.pending_changes(session, run.id)[0]
        held = workflow.claim(session, change.id, team["alice"])
        held.expires_at = utcnow() - timedelta(seconds=1)
        session.add(held)
        session.flush()

        workflow.claim(session, change.id, team["bob"])  # succeeds
        session.refresh(held)
        assert held.status is ClaimStatus.EXPIRED

    def test_reclaiming_extends_your_own_lease(self, session, run, team) -> None:
        change = review.pending_changes(session, run.id)[0]
        first = workflow.claim(session, change.id, team["alice"], lease_seconds=60)
        original = first.expires_at
        again = workflow.claim(session, change.id, team["alice"], lease_seconds=600)
        assert again.id == first.id and again.expires_at > original

    def test_an_unclaimed_change_is_fair_game(self, session, run, team) -> None:
        change = review.pending_changes(session, run.id)[0]
        review.record_decision(session, change.id, "accepted", "bob", participant=team["bob"])


class TestAssignment:
    def test_by_file_keeps_a_document_with_one_reviewer(self, session, run, team) -> None:
        workflow.auto_assign(session, run.id, [team["alice"], team["bob"]], strategy="by_file")
        owners: dict[int, set[str]] = {}
        for change in session.exec(select(Change).where(Change.run_id == run.id)).all():
            assignee = workflow.current_assignee(session, change.id)
            if assignee:
                owners.setdefault(change.document_id, set()).add(assignee.name)
        assert all(len(names) == 1 for names in owners.values())

    def test_work_is_spread_across_reviewers(self, session, run, team) -> None:
        spread = workflow.auto_assign(session, run.id, [team["alice"], team["bob"]])
        assert len(spread) == 2 and all(count > 0 for count in spread.values())

    def test_reassignment_is_append_only(self, session, run, team) -> None:
        change = review.pending_changes(session, run.id)[0]
        workflow.assign(session, run.id, [change.id], team["alice"])
        workflow.assign(session, run.id, [change.id], team["bob"])
        assert workflow.current_assignee(session, change.id).name == "bob"
        from termguard.models import Assignment

        rows = session.exec(
            select(Assignment).where(Assignment.change_id == change.id)
        ).all()
        assert len(rows) == 2  # the earlier routing is still on the record

    def test_an_unknown_strategy_is_refused(self, session, run, team) -> None:
        with pytest.raises(WorkflowError, match="unknown assignment strategy"):
            workflow.auto_assign(session, run.id, [team["alice"]], strategy="vibes")

    def test_assigning_to_nobody_is_refused(self, session, run) -> None:
        with pytest.raises(WorkflowError, match="no participants"):
            workflow.auto_assign(session, run.id, [])


class TestAgentDisposition:
    def test_agent_decides_only_what_policy_permits(self, session, run, team, policy) -> None:
        result = workflow.agent_dispose(session, run.id, team["agent"], policy)
        assert result.decided > 0
        assert result.left_to_humans > 0

        decided = session.exec(
            select(Decision).where(Decision.decided_by_kind == ActorKind.LLM)
        ).all()
        assert len(decided) == result.decided
        assert all(d.policy_clause_id for d in decided)
        assert all(d.policy_hash == policy.hash for d in decided)

    def test_judgment_calls_are_left_to_humans(self, session, run, team, policy) -> None:
        workflow.agent_dispose(session, run.id, team["agent"], policy)
        from termguard.models import Hit

        for decision in session.exec(
            select(Decision).where(Decision.decided_by_kind == ActorKind.LLM)
        ).all():
            change = session.get(Change, decision.change_id)
            hit = session.get(Hit, change.hit_id)
            assert hit.rule_id not in {"R-002", "R-003", "R-010"}

    def test_a_dry_run_records_nothing(self, session, run, team, policy) -> None:
        preview = workflow.agent_dispose(session, run.id, team["agent"], policy, dry_run=True)
        assert preview.decided > 0
        assert session.exec(select(Decision)).all() == []

    def test_a_human_cannot_be_passed_off_as_an_agent(self, session, run, team, policy) -> None:
        with pytest.raises(WorkflowError, match="is not an agent"):
            workflow.agent_dispose(session, run.id, team["alice"], policy)

    def test_an_agent_decision_without_a_clause_is_refused(self, session, run, team) -> None:
        """The whole argument rests on being able to name the authority for each one."""
        change = review.pending_changes(session, run.id)[0]
        with pytest.raises(ValueError, match="must name the policy clause"):
            review.record_decision(session, change.id, "accepted", "agent-1",
                                   participant=team["agent"])

    def test_confirmation_is_recorded_against_a_person(self, session, run, team, policy) -> None:
        workflow.agent_dispose(session, run.id, team["agent"], policy)
        pending = session.exec(
            select(Decision).where(Decision.requires_human_confirm == True)  # noqa: E712
        ).all()
        assert pending, "expected a clause requiring confirmation"

        count = workflow.confirm_agent_batch(session, run.id, team["alice"])
        assert count == len(pending)
        for decision in pending:
            session.refresh(decision)
            assert decision.confirmed_by == "alice"

    def test_an_agent_cannot_confirm_agent_work(self, session, run, team, policy) -> None:
        workflow.agent_dispose(session, run.id, team["agent"], policy)
        with pytest.raises(WorkflowError, match="agent cannot confirm"):
            workflow.confirm_agent_batch(session, run.id, team["agent"])


class TestSeparationOfDuties:
    def _clear(self, session, run, who) -> None:
        for change in list(review.pending_changes(session, run.id)):
            review.record_decision(session, change.id, "accepted", who.name, participant=who)

    def test_an_agent_can_never_sign_off(self, session, run, team) -> None:
        team["agent"].roles = ["reviewer", "approver"]  # even with the role
        session.add(team["agent"])
        ok, blockers = workflow.separation_of_duties(session, run.id, team["agent"])
        assert not ok
        assert any("agent cannot sign off" in b for b in blockers)

    def test_a_reviewer_of_this_run_cannot_approve_it(self, session, run, team) -> None:
        team["alice"].roles = ["reviewer", "approver"]
        session.add(team["alice"])
        self._clear(session, run, team["alice"])

        ok, blockers = workflow.separation_of_duties(session, run.id, team["alice"])
        assert not ok
        assert any("maker-checker" in b for b in blockers)

    def test_someone_without_the_approver_role_cannot_sign(self, session, run, team) -> None:
        ok, blockers = workflow.separation_of_duties(session, run.id, team["bob"])
        assert not ok
        assert any("approver role" in b for b in blockers)

    def test_an_uninvolved_approver_may_sign(self, session, run, team) -> None:
        ok, blockers = workflow.separation_of_duties(session, run.id, team["dana"])
        assert ok and blockers == []

    def test_all_blockers_are_reported_at_once(self, session, run, team) -> None:
        """Being told one reason at a time is a bad experience for a pre-flight check."""
        self._clear(session, run, team["bob"])
        ok, blockers = workflow.separation_of_duties(session, run.id, team["bob"])
        assert not ok and len(blockers) == 2


class TestSignOff:
    def _finish(self, session, run, store, wf_settings, team) -> None:
        for change in list(review.pending_changes(session, run.id)):
            review.record_decision(session, change.id, "accepted", "alice",
                                   participant=team["alice"])
        session.commit()
        verify.verify_run(session, run.id, settings=wf_settings, store=store,
                          write_outputs=False)

    def test_undecided_changes_block_approval(self, session, run, team, policy) -> None:
        with pytest.raises(WorkflowError, match="not ready"):
            workflow.sign_off(session, run.id, team["dana"], SignOffDecision.APPROVED,
                              policy=policy)

    def test_a_complete_verified_run_can_be_approved(
        self, session, run, team, store, wf_settings, policy
    ) -> None:
        self._finish(session, run, store, wf_settings, team)
        record = workflow.sign_off(session, run.id, team["dana"], SignOffDecision.APPROVED,
                                   note="reviewed", policy=policy)
        assert record.decision is SignOffDecision.APPROVED
        assert record.rulebook_hash == run.rulebook_hash
        assert record.policy_hash == policy.hash
        assert record.covered["decisions"] > 0

    def test_separation_of_duties_has_no_override(
        self, session, run, team, store, wf_settings, policy
    ) -> None:
        """force skips readiness, never the two-person rule."""
        self._finish(session, run, store, wf_settings, team)
        team["alice"].roles = ["reviewer", "approver"]
        session.add(team["alice"])
        with pytest.raises(WorkflowError, match="maker-checker"):
            workflow.sign_off(session, run.id, team["alice"], SignOffDecision.APPROVED,
                              policy=policy, force=True)

    def test_an_incomplete_run_can_still_be_rejected(self, session, run, team, policy) -> None:
        record = workflow.sign_off(session, run.id, team["dana"], SignOffDecision.REJECTED,
                                   note="sending it back", policy=policy)
        assert record.decision is SignOffDecision.REJECTED

    def test_readiness_lists_what_is_blocking(self, session, run, team) -> None:
        readiness = workflow.signoff_readiness(session, run.id)
        assert readiness["ready"] is False
        assert readiness["undecided"] > 0
        assert "dana" in readiness["eligible_approvers"]

    def test_sign_off_is_audited(self, session, run, team, store, wf_settings, policy) -> None:
        from termguard import audit

        self._finish(session, run, store, wf_settings, team)
        workflow.sign_off(session, run.id, team["dana"], SignOffDecision.APPROVED, policy=policy)
        events = [e for e in audit.events_for_run(session, run.id) if e.event == "run.approved"]
        assert events and events[-1].actor == "dana"
        assert events[-1].actor_kind is ActorKind.HUMAN


class TestMetrics:
    def test_agent_decisions_are_never_counted_as_model_accuracy(
        self, session, run, team, policy
    ) -> None:
        """An agent ruling on itself is delegated volume, not a human verdict."""
        workflow.agent_dispose(session, run.id, team["agent"], policy)
        session.commit()
        trust = metrics.ai_trust(session, run.id)
        assert trust["agent_decided"]["count"] > 0
        assert trust["judged_by_humans"] == 0
        assert trust["acceptance_rate"] is None  # no data, not zero percent

    def test_acceptance_rate_reflects_human_verdicts(self, session, run, team) -> None:
        from termguard.models import Mechanism

        ai_changes = session.exec(
            select(Change).where(Change.run_id == run.id, Change.mechanism == Mechanism.AI)
        ).all()
        for index, change in enumerate(ai_changes[:4]):
            kind = DecisionKind.REJECTED if index == 0 else DecisionKind.ACCEPTED
            review.record_decision(session, change.id, kind, "alice", participant=team["alice"])
        session.commit()

        trust = metrics.ai_trust(session, run.id)
        assert trust["judged_by_humans"] == 4
        assert trust["acceptance_rate"] == 0.75
        assert trust["override_rate"] == 0.25

    def test_automation_rate_counts_only_agent_decisions(
        self, session, run, team, policy
    ) -> None:
        workflow.agent_dispose(session, run.id, team["agent"], policy)
        for change in list(review.pending_changes(session, run.id)):
            review.record_decision(session, change.id, "accepted", "alice",
                                   participant=team["alice"])
        session.commit()

        flow = metrics.throughput(session, run.id)
        assert 0 < flow["automation_rate"] < 1
        assert flow["by_participant"]["agent-1"]["kind"] == "agent"
        assert flow["by_participant"]["alice"]["kind"] == "human"

    def test_rule_health_surfaces_overridden_rules(self, session, run, team) -> None:
        from termguard.models import Hit

        changes = session.exec(select(Change).where(Change.run_id == run.id)).all()
        target = next(
            c for c in changes
            if session.get(Hit, c.hit_id) and session.get(Hit, c.hit_id).rule_id == "R-001"
        )
        review.record_decision(session, target.id, "rejected", "alice",
                               participant=team["alice"])
        session.commit()

        health = {r["rule_id"]: r for r in metrics.rule_health(session, run.id)}
        assert health["R-001"]["override_rate"] == 1.0
        assert metrics.rule_health(session, run.id)[0]["rule_id"] == "R-001"  # sorted first

    def test_a_pace_is_not_invented_from_a_batch_script(self, session, run, team) -> None:
        for change in list(review.pending_changes(session, run.id)):
            review.record_decision(session, change.id, "accepted", "alice",
                                   participant=team["alice"])
        session.commit()
        assert metrics.throughput(session, run.id)["decisions_per_hour_estimate"] is None

    def test_attention_flags_pending_work(self, session, run) -> None:
        board = metrics.dashboard(session, run.id)
        titles = " ".join(item["title"] for item in board["attention"])
        assert "awaiting a decision" in titles

    def test_posture_counts_documents_and_versions(self, session, run) -> None:
        state = metrics.posture(session)
        assert state["documents"] == len(SUBSET)
        assert state["versions"] >= len(SUBSET)
