"""Layer D: agent authority.

The policy decides what a machine may dispose of. These tests are mostly about what it
must *refuse*, because a policy that only works when it says yes is not a control.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from termguard.policy import Clause, Policy, PolicyError, Risk, compute_hash, load_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY = REPO_ROOT / "data" / "policy.yaml"


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(body)
    return path


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy(POLICY)


class TestShippedPolicy:
    def test_loads_and_hashes(self, policy: Policy) -> None:
        assert len(policy) >= 1
        assert len(policy.hash) == 16
        assert policy.approved_by

    def test_hash_is_stable(self, policy: Policy) -> None:
        assert load_policy(POLICY).hash == policy.hash

    def test_hash_changes_with_the_risk_table(self, policy: Policy) -> None:
        """The risk table is part of the policy: changing it changes what agents may do."""
        mutated = dict(policy.risk_by_rule)
        mutated["R-002"] = Risk.LOW
        assert compute_hash(policy.clauses, mutated) != policy.hash

    def test_every_clause_explains_itself(self, policy: Policy) -> None:
        assert all(clause.rationale.strip() for clause in policy.clauses)

    def test_context_required_rules_are_never_delegated(self, policy: Policy) -> None:
        """R-002, R-003 and R-010 are judgment calls. Delegating them would undo containment."""
        delegated = {rule for clause in policy.clauses for rule in clause.rules}
        assert not ({"R-002", "R-003", "R-010"} & delegated)

    def test_judgment_rules_are_high_risk(self, policy: Policy) -> None:
        for rule_id in ("R-002", "R-003", "R-010"):
            assert policy.risk_of(rule_id) is Risk.HIGH


class TestAuthorization:
    def test_low_risk_orthography_is_delegated(self, policy: Policy) -> None:
        clause = policy.authorizes(
            rule_id="R-007", classification="unambiguous",
            mechanism="deterministic", part="body",
        )
        assert clause is not None and clause.id == "P-001"

    def test_a_judgment_call_is_never_delegated(self, policy: Policy) -> None:
        assert policy.authorizes(
            rule_id="R-002", classification="needs_judgment", mechanism="ai", part="body",
        ) is None

    def test_an_unknown_rule_defaults_to_a_human(self, policy: Policy) -> None:
        """Adding a rule to the rulebook must never silently widen agent authority."""
        assert policy.risk_of("R-999") is Risk.HIGH
        assert policy.authorizes(
            rule_id="R-999", classification="unambiguous",
            mechanism="deterministic", part="body",
        ) is None

    def test_medium_risk_in_the_body_is_not_delegated(self, policy: Policy) -> None:
        assert policy.authorizes(
            rule_id="R-001", classification="unambiguous",
            mechanism="deterministic", part="body",
        ) is None

    def test_the_same_rule_in_a_header_is_delegated_with_confirmation(self, policy) -> None:
        clause = policy.authorizes(
            rule_id="R-001", classification="unambiguous",
            mechanism="deterministic", part="header",
        )
        assert clause is not None and clause.requires_human_confirm is True

    def test_an_ai_mechanism_is_not_covered_by_a_deterministic_clause(self, policy) -> None:
        assert policy.authorizes(
            rule_id="R-007", classification="unambiguous", mechanism="ai", part="body",
        ) is None

    def test_a_missing_policy_file_delegates_nothing(self, tmp_path: Path) -> None:
        empty = load_policy(tmp_path / "absent.yaml")
        assert len(empty) == 0
        assert empty.authorizes(
            rule_id="R-007", classification="unambiguous",
            mechanism="deterministic", part="body",
        ) is None


class TestValidation:
    def test_a_clause_without_a_rationale_is_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
agent_may_decide:
  - {id: P-001, rules: [R-001]}
""")
        with pytest.raises(PolicyError, match="rationale"):
            load_policy(path)

    def test_duplicate_clause_ids_are_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
agent_may_decide:
  - {id: P-001, rules: [R-001], rationale: first}
  - {id: P-001, rules: [R-002], rationale: second}
""")
        with pytest.raises(PolicyError, match="duplicate clause id"):
            load_policy(path)

    def test_a_clause_cannot_authorize_an_arbitrary_decision(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
agent_may_decide:
  - {id: P-001, rules: [R-001], decision: edited, rationale: rewrite the text}
""")
        with pytest.raises(PolicyError):
            load_policy(path)

    def test_bad_id_format_is_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
agent_may_decide:
  - {id: CLAUSE1, rules: [R-001], rationale: x}
""")
        with pytest.raises(PolicyError):
            load_policy(path)

    def test_unknown_field_is_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
agent_may_decide:
  - {id: P-001, rules: [R-001], rationale: x, unlimited: true}
""")
        with pytest.raises(PolicyError):
            load_policy(path)


class TestClauseMatching:
    def test_empty_rules_means_any_rule(self) -> None:
        clause = Clause(id="P-001", rules=[], rationale="any")
        assert clause.covers(rule_id="R-042", classification="unambiguous",
                             mechanism="deterministic", part="body", risk=Risk.LOW)

    def test_risk_above_the_ceiling_is_not_covered(self) -> None:
        clause = Clause(id="P-001", rules=[], max_risk=Risk.LOW, rationale="low only")
        assert not clause.covers(rule_id="R-001", classification="unambiguous",
                                 mechanism="deterministic", part="body", risk=Risk.MEDIUM)

    def test_part_restriction_is_honoured(self) -> None:
        clause = Clause(id="P-001", rules=[], parts=["header"], rationale="headers only")
        assert clause.covers(rule_id="R-001", classification="unambiguous",
                             mechanism="deterministic", part="header", risk=Risk.LOW)
        assert not clause.covers(rule_id="R-001", classification="unambiguous",
                                 mechanism="deterministic", part="body", risk=Risk.LOW)

    def test_first_matching_clause_wins(self) -> None:
        narrow = Clause(id="P-001", rules=["R-007"], rationale="narrow")
        broad = Clause(id="P-002", rules=[], rationale="broad")
        book = Policy(clauses=[narrow, broad], risk_by_rule={"R-007": Risk.LOW})
        assert book.authorizes(rule_id="R-007", classification="unambiguous",
                               mechanism="deterministic", part="body").id == "P-001"
