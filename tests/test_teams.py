"""Layer D: organizations, teams, routing, handoff and cover.

Work moving between people is the point, so most of these assert on *movement* — where a
change ends up, whose queue it leaves, and what the record says about why.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from termguard import review, teams, workflow
from termguard.models import (
    Change,
    Handoff,
    HandoffKind,
    Hit,
    Membership,
    ParticipantKind,
    Role,
    TeamRole,
    utcnow,
)
from termguard.pipeline import run_pipeline
from termguard.rulebook import load_rulebook
from termguard.teams import TeamError
from termguard.workflow import WorkflowError

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBSET = ("IFU-001.docx", "RMS-001.docx")


@pytest.fixture(scope="module")
def rulebook():
    return load_rulebook(REPO_ROOT / "data" / "rulebook.yaml")


@pytest.fixture
def team_settings(settings, corpus_dir: Path, tmp_path: Path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in SUBSET:
        shutil.copy(corpus_dir / name, corpus / name)
    return replace(settings, corpus_dir=corpus)


@pytest.fixture
def run(session, store, team_settings):
    result, _ = run_pipeline(session, settings=team_settings, store=store, dry_run=True)
    return result


@pytest.fixture
def org_teams(session, rulebook):
    org = teams.ensure_org(session, "Meridian Medical", "meridian")
    return teams.teams_from_rulebook(session, rulebook, org)


@pytest.fixture
def crew(session, org_teams):
    people = {
        "alice": workflow.ensure_participant(session, "alice", roles=[Role.REVIEWER]),
        "bob": workflow.ensure_participant(session, "bob", roles=[Role.REVIEWER]),
        "carol": workflow.ensure_participant(session, "carol", roles=[Role.REVIEWER]),
        "agent": workflow.ensure_participant(
            session, "bot", kind=ParticipantKind.AGENT, roles=[Role.REVIEWER]
        ),
    }
    teams.add_member(session, org_teams["clinical-affairs"], people["alice"])
    teams.add_member(session, org_teams["clinical-affairs"], people["bob"], role=TeamRole.LEAD)
    teams.add_member(session, org_teams["regulatory-affairs"], people["carol"], role=TeamRole.LEAD)
    return people


class TestTeamsFromRulebook:
    def test_one_team_per_rule_owner(self, session, rulebook, org_teams) -> None:
        """The rulebook already records who owns each rule; the team list derives from it."""
        assert set(org_teams) == {r.owner for r in rulebook if r.owner}
        assert "clinical-affairs" in org_teams

    def test_teams_describe_the_rules_they_own(self, org_teams) -> None:
        assert "R-002" in org_teams["clinical-affairs"].description

    def test_seeding_is_idempotent(self, session, rulebook, org_teams) -> None:
        again = teams.teams_from_rulebook(session, rulebook)
        assert {t.id for t in again.values()} == {t.id for t in org_teams.values()}

    def test_every_owner_has_a_team(self, session, rulebook, org_teams) -> None:
        assert teams.unrouted_owners(session, rulebook) == {}

    def test_a_missing_team_is_reported_not_hidden(self, session, rulebook) -> None:
        """Work with nowhere to go must surface, not land in a default queue."""
        teams.ensure_team(session, "clinical-affairs")
        missing = teams.unrouted_owners(session, rulebook)
        assert "regulatory-affairs" in missing
        assert "R-001" in missing["regulatory-affairs"]

    def test_team_for_rule_resolves_the_owner(self, session, rulebook, org_teams) -> None:
        assert teams.team_for_rule(session, rulebook, "R-002").slug == "clinical-affairs"

    def test_unknown_team_raises(self, session) -> None:
        with pytest.raises(TeamError, match="unknown team"):
            teams.get_team(session, "no-such-team")


class TestMembership:
    def test_members_and_leads(self, session, org_teams, crew) -> None:
        clinical = org_teams["clinical-affairs"]
        assert {p.name for p in teams.members(session, clinical)} == {"alice", "bob"}
        assert [p.name for p in teams.leads(session, clinical)] == ["bob"]

    def test_a_participant_can_be_on_several_teams(self, session, org_teams, crew) -> None:
        teams.add_member(session, org_teams["quality-assurance"], crew["alice"])
        assert len(teams.teams_of(session, crew["alice"])) == 2

    def test_role_changes_in_place(self, session, org_teams, crew) -> None:
        teams.add_member(session, org_teams["clinical-affairs"], crew["alice"],
                         role=TeamRole.LEAD)
        assert {p.name for p in teams.leads(session, org_teams["clinical-affairs"])} == {
            "alice", "bob"
        }
        rows = session.exec(
            select(Membership).where(Membership.participant_id == crew["alice"].id)
        ).all()
        assert len(rows) == 1  # updated, not duplicated

    def test_leaving_records_a_date_rather_than_deleting(self, session, org_teams, crew) -> None:
        clinical = org_teams["clinical-affairs"]
        assert teams.remove_member(session, clinical, crew["alice"]) is True
        assert {p.name for p in teams.members(session, clinical)} == {"bob"}
        row = session.exec(
            select(Membership).where(Membership.participant_id == crew["alice"].id)
        ).one()
        assert row.left_at is not None

    def test_removing_a_non_member_is_a_no_op(self, session, org_teams, crew) -> None:
        assert teams.remove_member(session, org_teams["quality-assurance"], crew["alice"]) is False


class TestRouting:
    def test_changes_reach_the_team_that_owns_the_rule(
        self, session, run, rulebook, org_teams, crew
    ) -> None:
        result = workflow.route_by_rule_owner(session, run.id, rulebook)
        assert result["total"] > 0
        assert result["unroutable"] == 0

        for change in session.exec(select(Change).where(Change.run_id == run.id)).all():
            assignment = workflow.current_assignment(session, change.id)
            if assignment is None or assignment.team_id is None:
                continue
            hit = session.get(Hit, change.hit_id)
            expected = teams.team_for_rule(session, rulebook, hit.rule_id)
            assert assignment.team_id == expected.id

    def test_a_pooled_change_has_no_named_assignee(
        self, session, run, rulebook, org_teams, crew
    ) -> None:
        workflow.route_by_rule_owner(session, run.id, rulebook)
        change_id = workflow.queue_for(session, run.id, crew["alice"])[0]
        assert workflow.current_assignee(session, change_id) is None
        assert workflow.change_state(session, change_id) == "pooled"

    def test_any_team_member_sees_the_pool(self, session, run, rulebook, org_teams, crew) -> None:
        workflow.route_by_rule_owner(session, run.id, rulebook)
        assert workflow.queue_for(session, run.id, crew["alice"]) == \
               workflow.queue_for(session, run.id, crew["bob"])

    def test_someone_on_no_team_sees_nothing(self, session, run, rulebook, org_teams, crew) -> None:
        workflow.route_by_rule_owner(session, run.id, rulebook)
        outsider = workflow.ensure_participant(session, "outsider", roles=[Role.REVIEWER])
        assert workflow.queue_for(session, run.id, outsider) == []

    def test_routing_does_not_override_a_pinned_owner(
        self, session, run, rulebook, org_teams, crew
    ) -> None:
        """A deliberate assignment must survive a re-route."""
        workflow.route_by_rule_owner(session, run.id, rulebook)
        change_id = workflow.queue_for(session, run.id, crew["alice"])[0]
        workflow.reassign(session, run.id, change_id, actor=crew["alice"],
                          to_participant=crew["carol"], reason="carol knows this document")

        workflow.route_by_rule_owner(session, run.id, rulebook)
        assert workflow.current_assignee(session, change_id).name == "carol"

    def test_routing_is_audited(self, session, run, rulebook, org_teams, crew) -> None:
        from termguard import audit

        workflow.route_by_rule_owner(session, run.id, rulebook)
        events = [e for e in audit.events_for_run(session, run.id)
                  if e.event == "changes.route"]
        assert events


class TestHandoffs:
    @pytest.fixture
    def routed(self, session, run, rulebook, org_teams, crew):
        workflow.route_by_rule_owner(session, run.id, rulebook)
        return workflow.queue_for(session, run.id, crew["alice"])[0]

    def test_escalation_goes_to_the_team_lead(self, session, run, crew, routed) -> None:
        workflow.escalate(session, run.id, routed, actor=crew["alice"],
                          reason="cannot tell if this is patient-facing")
        assert workflow.current_assignee(session, routed).name == "bob"
        assert workflow.change_state(session, routed) == "escalated"

    def test_an_escalated_change_is_visibly_open_not_merely_undecided(
        self, session, run, crew, routed
    ) -> None:
        """Undecided and escalated must not look the same; that is how hard cases rot."""
        assert workflow.change_state(session, routed) == "pooled"
        workflow.escalate(session, run.id, routed, actor=crew["alice"], reason="unclear")
        assert workflow.change_state(session, routed) == "escalated"

    def test_escalation_without_a_lead_is_refused(self, session, run, rulebook, crew) -> None:
        """A team with nobody to escalate to should say so, not pick someone at random."""
        lonely = teams.ensure_team(session, "no-leads")
        solo = workflow.ensure_participant(session, "solo", roles=[Role.REVIEWER])
        teams.add_member(session, lonely, solo)

        change = session.exec(select(Change).where(Change.run_id == run.id)).first()
        workflow.assign_to_team(session, run.id, [change.id], lonely, reason="test")

        with pytest.raises(WorkflowError, match="no team lead"):
            workflow.escalate(session, run.id, change.id, actor=solo, reason="help")

    def test_reassignment_pins_the_new_owner(self, session, run, crew, routed) -> None:
        workflow.reassign(session, run.id, routed, actor=crew["alice"],
                          to_participant=crew["carol"], reason="regulatory reading")
        assert workflow.current_assignee(session, routed).name == "carol"
        assert workflow.current_assignment(session, routed).pinned is True

    def test_reassignment_to_a_team_returns_it_to_a_pool(self, session, run, org_teams,
                                                         crew, routed) -> None:
        workflow.reassign(session, run.id, routed, actor=crew["alice"],
                          to_team=org_teams["regulatory-affairs"], reason="wrong team")
        assert workflow.change_state(session, routed) == "pooled"
        assert routed in workflow.queue_for(session, run.id, crew["carol"])

    @pytest.mark.parametrize("operation", ["reassign", "escalate", "return"])
    def test_every_handoff_demands_a_reason(self, session, run, org_teams, crew,
                                            routed, operation: str) -> None:
        """Work changing hands silently is the thing this record exists to prevent."""
        kwargs = {"actor": crew["alice"], "reason": "   "}
        with pytest.raises(WorkflowError):
            if operation == "reassign":
                workflow.reassign(session, run.id, routed, to_participant=crew["carol"], **kwargs)
            elif operation == "escalate":
                workflow.escalate(session, run.id, routed, **kwargs)
            else:
                workflow.return_for_clarification(
                    session, run.id, routed, to_team=org_teams["regulatory-affairs"], **kwargs
                )

    def test_reassign_needs_a_destination(self, session, run, crew, routed) -> None:
        with pytest.raises(WorkflowError, match="destination"):
            workflow.reassign(session, run.id, routed, actor=crew["alice"], reason="somewhere")

    def test_a_handoff_releases_the_previous_holders_lease(
        self, session, run, crew, routed
    ) -> None:
        """The lease belongs to whoever was working it; it must not travel with the change."""
        workflow.claim(session, routed, crew["alice"])
        workflow.reassign(session, run.id, routed, actor=crew["alice"],
                          to_participant=crew["carol"], reason="handing over")
        assert workflow.active_claim(session, routed) is None
        workflow.claim(session, routed, crew["carol"])  # carol is not blocked

    def test_history_records_every_movement(self, session, run, org_teams, crew, routed) -> None:
        workflow.escalate(session, run.id, routed, actor=crew["alice"], reason="unclear")
        workflow.reassign(session, run.id, routed, actor=crew["bob"],
                          to_participant=crew["carol"], reason="regulatory owns it")
        kinds = [h.kind for h in workflow.handoffs(session, routed)]
        assert kinds == [HandoffKind.ROUTE, HandoffKind.ESCALATE, HandoffKind.REASSIGN]
        reasons = [h.reason for h in workflow.handoffs(session, routed)]
        assert "regulatory owns it" in reasons


class TestReturnForClarification:
    @pytest.fixture
    def routed(self, session, run, rulebook, org_teams, crew):
        workflow.route_by_rule_owner(session, run.id, rulebook)
        return workflow.queue_for(session, run.id, crew["alice"])[0]

    def test_a_returned_change_leaves_the_queue(self, session, run, org_teams,
                                                crew, routed) -> None:
        workflow.return_for_clarification(
            session, run.id, routed, actor=crew["alice"],
            to_team=org_teams["regulatory-affairs"], reason="is this quoting 21 CFR?",
        )
        assert workflow.change_state(session, routed) == "returned"
        assert routed not in workflow.queue_for(session, run.id, crew["alice"])
        assert routed not in workflow.queue_for(session, run.id, crew["carol"])

    def test_resolving_puts_it_back(self, session, run, org_teams, crew, routed) -> None:
        workflow.return_for_clarification(
            session, run.id, routed, actor=crew["alice"],
            to_team=org_teams["regulatory-affairs"], reason="question?",
        )
        workflow.resolve_return(session, run.id, routed, actor=crew["carol"],
                                answer="yes, verbatim quote")
        assert workflow.change_state(session, routed) != "returned"
        assert workflow.open_return(session, routed) is None

    def test_resolving_nothing_is_refused(self, session, run, crew, routed) -> None:
        with pytest.raises(WorkflowError, match="no outstanding return"):
            workflow.resolve_return(session, run.id, routed, actor=crew["carol"], answer="x")

    def test_the_question_and_answer_are_both_recorded(self, session, run, org_teams,
                                                        crew, routed) -> None:
        workflow.return_for_clarification(
            session, run.id, routed, actor=crew["alice"],
            to_team=org_teams["regulatory-affairs"], reason="which CFR section?",
        )
        workflow.resolve_return(session, run.id, routed, actor=crew["carol"],
                                answer="820.180")
        history = workflow.handoffs(session, routed)
        assert history[-2].reason == "which CFR section?"
        assert history[-1].reason == "820.180"
        assert history[-1].resolves_handoff_id == history[-2].id

    def test_a_returned_change_still_blocks_the_gate(self, session, run, org_teams,
                                                     crew, routed) -> None:
        """Returned is not decided. Verification must not pass over an open question."""
        workflow.return_for_clarification(
            session, run.id, routed, actor=crew["alice"],
            to_team=org_teams["regulatory-affairs"], reason="question?",
        )
        readiness = workflow.signoff_readiness(session, run.id)
        assert readiness["ready"] is False
        assert readiness["undecided"] > 0


class TestCover:
    def test_cover_moves_pinned_work(self, session, run, rulebook, org_teams, crew) -> None:
        workflow.route_by_rule_owner(session, run.id, rulebook)
        change_id = workflow.queue_for(session, run.id, crew["alice"])[0]
        workflow.reassign(session, run.id, change_id, actor=crew["alice"],
                          to_participant=crew["alice"], reason="mine")

        assert change_id not in workflow.queue_for(session, run.id, crew["carol"])
        teams.delegate(session, crew["alice"], crew["carol"], reason="leave")
        assert change_id in workflow.queue_for(session, run.id, crew["carol"])

    def test_revoking_cover_returns_the_work(self, session, run, rulebook,
                                             org_teams, crew) -> None:
        workflow.route_by_rule_owner(session, run.id, rulebook)
        change_id = workflow.queue_for(session, run.id, crew["alice"])[0]
        workflow.reassign(session, run.id, change_id, actor=crew["alice"],
                          to_participant=crew["alice"], reason="mine")
        teams.delegate(session, crew["alice"], crew["carol"], reason="leave")
        teams.revoke_delegation(session, crew["alice"])
        assert change_id not in workflow.queue_for(session, run.id, crew["carol"])

    def test_cover_follows_a_chain(self, session, run, rulebook, org_teams, crew) -> None:
        """Alice covered by Bob, Bob covered by Carol: Alice's work is Carol's problem."""
        workflow.route_by_rule_owner(session, run.id, rulebook)
        change_id = workflow.queue_for(session, run.id, crew["alice"])[0]
        workflow.reassign(session, run.id, change_id, actor=crew["alice"],
                          to_participant=crew["alice"], reason="mine")

        teams.delegate(session, crew["alice"], crew["bob"], reason="leave")
        teams.delegate(session, crew["bob"], crew["carol"], reason="also out")

        assert teams.effective_owner(session, crew["alice"]).name == "carol"
        assert change_id in workflow.queue_for(session, run.id, crew["carol"])

    def test_an_expired_cover_lapses_on_its_own(self, session, run, rulebook,
                                                org_teams, crew) -> None:
        row = teams.delegate(session, crew["alice"], crew["carol"],
                             until=utcnow() + timedelta(days=1), reason="short trip")
        assert teams.effective_owner(session, crew["alice"]).name == "carol"
        row.ends_at = utcnow() - timedelta(seconds=1)
        session.add(row)
        session.flush()
        assert teams.effective_owner(session, crew["alice"]).name == "alice"

    def test_an_agent_cannot_cover_for_a_person(self, session, crew) -> None:
        """Cover for an absent human must not become machine authority."""
        with pytest.raises(TeamError, match="cover must be a human"):
            teams.delegate(session, crew["alice"], crew["agent"])

    def test_self_delegation_is_refused(self, session, crew) -> None:
        with pytest.raises(TeamError, match="cannot delegate to themselves"):
            teams.delegate(session, crew["alice"], crew["alice"])

    def test_a_cycle_does_not_hang_the_queue(self, session, crew) -> None:
        """A hung queue is worse than cover landing one hop short."""
        teams.delegate(session, crew["alice"], crew["bob"], reason="a")
        teams.delegate(session, crew["bob"], crew["alice"], reason="b")
        assert teams.effective_owner(session, crew["alice"]) is not None

    def test_describe_reports_both_directions(self, session, crew) -> None:
        teams.delegate(session, crew["alice"], crew["carol"], reason="leave")
        assert teams.describe(session, crew["alice"])["covered_by"] == "carol"
        assert "alice" in teams.describe(session, crew["carol"])["covering_for"]
