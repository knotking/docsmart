"""Layer A: rulebook loading, validation and casing."""

from __future__ import annotations

from pathlib import Path

import pytest

from termguard.rulebook import (
    CaseKind,
    MatchKind,
    Rule,
    RuleError,
    Scope,
    compute_hash,
    load_rulebook,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RULEBOOK = REPO_ROOT / "data" / "rulebook.yaml"


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rb.yaml"
    path.write_text(body)
    return path


class TestShippedRulebook:
    def test_loads_with_twelve_rules(self) -> None:
        rb = load_rulebook(RULEBOOK)
        assert len(rb) == 12
        assert len({r.id for r in rb}) == 12

    def test_meets_the_specified_composition(self) -> None:
        rb = load_rulebook(RULEBOOK)
        assert len(rb.context_rules) >= 2
        assert len([r for r in rb if r.exceptions]) >= 2
        assert len([r for r in rb if r.case is CaseKind.PRESERVE]) >= 2

    def test_hash_is_stable_and_order_independent(self) -> None:
        rb = load_rulebook(RULEBOOK)
        assert rb.hash == load_rulebook(RULEBOOK).hash
        assert compute_hash(list(reversed(rb.rules))) == rb.hash

    def test_hash_changes_when_a_rule_changes(self) -> None:
        rb = load_rulebook(RULEBOOK)
        mutated = [r.model_copy(update={"approved": "something else"}) if r.id == "R-001" else r
                   for r in rb.rules]
        assert compute_hash(mutated) != rb.hash


class TestValidation:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(RuleError, match="not found"):
            load_rulebook(tmp_path / "nope.yaml")

    def test_missing_rules_key(self, tmp_path: Path) -> None:
        with pytest.raises(RuleError, match="top-level 'rules' key"):
            load_rulebook(write(tmp_path, "version: '1'\n"))

    def test_bad_id_format_names_the_rule(self, tmp_path: Path) -> None:
        path = write(tmp_path, "rules:\n  - id: NOPE\n    deprecated: [x]\n    approved: y\n")
        with pytest.raises(RuleError, match="NOPE"):
            load_rulebook(path)

    def test_duplicate_ids_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
rules:
  - {id: R-001, deprecated: [alpha], approved: A}
  - {id: R-001, deprecated: [beta], approved: B}
""")
        with pytest.raises(RuleError, match="duplicate rule id"):
            load_rulebook(path)

    def test_overlapping_terms_rejected_with_both_rule_ids(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
rules:
  - {id: R-001, deprecated: [physician], approved: healthcare provider}
  - {id: R-002, deprecated: [Physician], approved: clinician}
""")
        with pytest.raises(RuleError) as exc:
            load_rulebook(path)
        assert "R-001" in str(exc.value) and "R-002" in str(exc.value)

    def test_bad_regex_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
rules:
  - {id: R-001, deprecated: ['[unclosed'], approved: X, match: regex}
""")
        with pytest.raises(RuleError, match="bad pattern"):
            load_rulebook(path)

    def test_context_required_without_a_note_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
rules:
  - {id: R-001, deprecated: [x], approved: y, context_required: true}
""")
        with pytest.raises(RuleError, match="context_note"):
            load_rulebook(path)

    def test_unknown_field_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, """
rules:
  - {id: R-001, deprecated: [x], approved: y, tpyo: true}
""")
        with pytest.raises(RuleError):
            load_rulebook(path)

    def test_regex_rules_may_overlap(self, tmp_path: Path) -> None:
        """Deciding regex intersection is not worth the false alarms; only literals collide."""
        path = write(tmp_path, """
rules:
  - {id: R-001, deprecated: ['\\\\d+ ml'], approved: mL, match: regex}
  - {id: R-002, deprecated: ['\\\\d+ mg'], approved: mg, match: regex}
""")
        assert len(load_rulebook(path)) == 2


class TestMatching:
    def test_whole_word_does_not_match_inside_a_longer_word(self) -> None:
        rule = Rule(id="R-001", deprecated=["IFU"], approved="Instructions for Use",
                    match=MatchKind.WHOLE_WORD, case=CaseKind.EXACT)
        pattern = rule.compiled()[0]
        assert pattern.search("see the IFU for details")
        assert not pattern.search("multiple IFUs exist")

    def test_phrase_tolerates_extra_whitespace(self) -> None:
        rule = Rule(id="R-001", deprecated=["Meridian Pump 2"], approved="X", match=MatchKind.PHRASE)
        assert rule.compiled()[0].search("the Meridian  Pump\n2 device")

    def test_case_exact_is_case_sensitive(self) -> None:
        rule = Rule(id="R-007", deprecated=["ml"], approved="mL", case=CaseKind.EXACT)
        pattern = rule.compiled()[0]
        assert pattern.search("250 ml")
        assert not pattern.search("250 ML")


class TestCasePreservation:
    @pytest.mark.parametrize(
        "matched,expected",
        [
            ("physician", "healthcare provider"),
            ("Physician", "Healthcare provider"),   # sentence-initial, not Title Case
            ("PHYSICIAN", "HEALTHCARE PROVIDER"),
        ],
    )
    def test_single_word(self, matched: str, expected: str) -> None:
        rule = Rule(id="R-004", deprecated=["physician"], approved="healthcare provider")
        assert rule.render_replacement(matched) == expected

    def test_multiword_title_case_propagates(self) -> None:
        """A Title Case heading fragment keeps Title Case."""
        rule = Rule(id="R-001", deprecated=["Meridian Pump 2"], approved="meridian infusion system",
                    match=MatchKind.PHRASE)
        assert rule.render_replacement("Meridian Pump 2") == "Meridian Infusion System"

    def test_exact_and_insensitive_replace_verbatim(self) -> None:
        for kind in (CaseKind.EXACT, CaseKind.INSENSITIVE):
            rule = Rule(id="R-007", deprecated=["ml"], approved="mL", case=kind)
            assert rule.render_replacement("ML") == "mL"


class TestScope:
    def test_header_hit_needs_headers_footers_scope(self) -> None:
        body_only = Rule(id="R-005", deprecated=["shall"], approved="must", scope=[Scope.BODY])
        assert not body_only.applies_to(part="header", is_heading=False, in_table=False)
        assert body_only.applies_to(part="body", is_heading=False, in_table=False)

    def test_heading_and_table_scopes_are_independent(self) -> None:
        rule = Rule(id="R-005", deprecated=["shall"], approved="must",
                    scope=[Scope.BODY, Scope.TABLES])
        assert rule.applies_to(part="body", is_heading=False, in_table=True)
        assert not rule.applies_to(part="body", is_heading=True, in_table=False)

    def test_default_scope_is_everything(self) -> None:
        rule = Rule(id="R-001", deprecated=["x"], approved="y")
        for part in ("body", "header", "footer", "footnote", "endnote", "textbox"):
            assert rule.applies_to(part=part, is_heading=False, in_table=False)
