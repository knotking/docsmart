"""Layer C (AI): containment of the LLM step.

No test in this file calls the API. The point of these tests is not that the model behaves
well - it is that the code rejects the model when it does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from termguard.judge import (
    DECISIONS,
    PROMPT_VERSION,
    Judge,
    JudgeError,
    ResponseCache,
    build_prompts,
    detect_over_edit,
    load_fixtures,
    parse_response,
    prompt_hash,
    validate,
)
from termguard.rulebook import Rulebook, load_rulebook
from termguard.scanner import Hit
from termguard.walker import Location

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "judge"

CLINICAL_SENTENCE = "Investigators recorded each side effect observed during the study period."
PATIENT_SENTENCE = "Tell your care team about any side effect you notice."


@pytest.fixture(scope="module")
def rulebook() -> Rulebook:
    return load_rulebook(REPO_ROOT / "data" / "rulebook.yaml")


def make_hit(
    sentence: str = CLINICAL_SENTENCE,
    *,
    rule_id: str = "R-002",
    matched: str = "side effect",
    approved: str = "adverse event",
    paragraph: str | None = None,
    is_heading: bool = False,
    part: str = "body",
) -> Hit:
    paragraph = paragraph if paragraph is not None else sentence
    start = sentence.find(matched)
    return Hit(
        location=Location(file="t.docx", part=part, part_name="word/document.xml",
                          paragraph_index=0, is_heading=is_heading),
        rule_id=rule_id, matched_text=matched, approved_text=approved,
        span=(start, start + len(matched)), occurrence=0, sentence=sentence,
        paragraph_text=paragraph, classification="needs_judgment",
        reason="rule is context_required",
    )


def response(decision: str, revised: str, justification: str = "because.") -> str:
    return json.dumps(
        {"decision": decision, "revised_sentence": revised, "justification": justification}
    )


def run_validate(hit: Hit, raw: str):
    return validate(hit, raw, model="test-model", prompt_version=PROMPT_VERSION, hash_="abc123")


class TestContainment:
    """Constraint 3: the model never sees more than one sentence and its paragraph."""

    def test_prompt_contains_only_the_sentence_and_its_paragraph(self, rulebook) -> None:
        hit = make_hit(paragraph=CLINICAL_SENTENCE + " Another sentence follows.")
        system, user = build_prompts(hit, rulebook.get("R-002"))
        assert CLINICAL_SENTENCE in user
        assert hit.paragraph_text in user
        # Nothing resembling a whole document is present.
        assert len(user) < 4000
        assert "Meridian Pump 2" not in user

    def test_prompt_states_exactly_one_rule(self, rulebook) -> None:
        system, _ = build_prompts(make_hit(), rulebook.get("R-002"))
        assert "R-002" in system
        assert "adverse event" in system
        for other in ("R-001", "R-004", "R-009"):
            assert other not in system

    def test_prompt_carries_the_context_guidance(self, rulebook) -> None:
        system, _ = build_prompts(make_hit(), rulebook.get("R-002"))
        assert "patient-facing" in system.lower()

    def test_prompt_forbids_editing_outside_the_span(self, rulebook) -> None:
        system, _ = build_prompts(make_hit(), rulebook.get("R-002"))
        assert "ONLY the flagged span" in system
        assert "will be rejected" in system

    def test_heading_context_is_surfaced_to_the_model(self, rulebook) -> None:
        hit = make_hit(paragraph="What You May Experience", is_heading=True)
        _, user = build_prompts(hit, rulebook.get("R-002"))
        assert "heading" in user

    def test_judge_all_refuses_non_judgment_hits(self, rulebook) -> None:
        hit = make_hit()
        unambiguous = Hit(**{**hit.__dict__, "classification": "unambiguous"})
        with pytest.raises(JudgeError, match="constraint 3"):
            Judge(rulebook, live=False).judge_all([unambiguous])

    def test_prompt_hash_is_stable_and_versioned(self, rulebook) -> None:
        system, _ = build_prompts(make_hit(), rulebook.get("R-002"))
        assert prompt_hash(system) == prompt_hash(system)
        assert len(prompt_hash(system)) == 16


class TestOverEditRejection:
    """The check that stops a model quietly improving a regulated sentence."""

    def test_minimal_swap_is_accepted(self) -> None:
        hit = make_hit()
        outcome = run_validate(hit, response(
            "change", "Investigators recorded each adverse event observed during the study period."
        ))
        assert outcome.decision == "change"
        assert not outcome.was_rejected

    def test_reworded_sentence_is_rejected(self) -> None:
        hit = make_hit()
        outcome = run_validate(hit, response(
            "change",
            "Investigators carefully logged every adverse event seen throughout the trial.",
        ))
        assert outcome.decision == "escalate"
        assert "over-edit" in outcome.rejected_reason
        assert outcome.raw_decision == "change"

    def test_edit_to_the_tail_of_the_sentence_is_rejected(self) -> None:
        hit = make_hit()
        outcome = run_validate(hit, response(
            "change",
            "Investigators recorded each adverse event observed during the trial window.",
        ))
        assert outcome.decision == "escalate"
        assert "over-edit" in outcome.rejected_reason

    def test_article_agreement_is_permitted(self) -> None:
        sentence = "Report a side effect promptly."
        hit = make_hit(sentence)
        outcome = run_validate(hit, response("change", "Report an adverse event promptly."))
        assert outcome.decision == "change"

    def test_plural_agreement_is_permitted(self) -> None:
        sentence = "Common side effects include redness."
        hit = make_hit(sentence, matched="side effects")
        outcome = run_validate(hit, response("change", "Common adverse events include redness."))
        assert outcome.decision == "change"

    def test_rejection_reason_names_what_was_touched(self) -> None:
        hit = make_hit()
        outcome = run_validate(hit, response(
            "change",
            "Investigators recorded each adverse event observed during the trial window.",
        ))
        assert "study period" in outcome.rejected_reason


class TestResponseValidation:
    def test_malformed_json_escalates(self) -> None:
        outcome = run_validate(make_hit(), '{"decision": "change", "revised_sentence": trunc')
        assert outcome.decision == "escalate"
        assert "not valid JSON" in outcome.rejected_reason

    def test_non_object_json_escalates(self) -> None:
        outcome = run_validate(make_hit(), '["change"]')
        assert outcome.decision == "escalate"
        assert "not a JSON object" in outcome.rejected_reason

    def test_missing_field_escalates(self) -> None:
        outcome = run_validate(make_hit(), '{"decision": "change", "justification": "x"}')
        assert outcome.decision == "escalate"
        assert "missing" in outcome.rejected_reason

    def test_unknown_decision_escalates(self) -> None:
        outcome = run_validate(make_hit(), response("rewrite", CLINICAL_SENTENCE))
        assert outcome.decision == "escalate"
        assert "unknown decision" in outcome.rejected_reason

    def test_change_without_the_approved_term_is_rejected(self) -> None:
        outcome = run_validate(make_hit(), response("change", CLINICAL_SENTENCE))
        assert outcome.decision == "escalate"
        assert "absent" in outcome.rejected_reason

    def test_keep_that_edits_the_sentence_is_rejected(self) -> None:
        outcome = run_validate(make_hit(), response(
            "keep", "Investigators recorded each event observed during the study."
        ))
        assert outcome.decision == "escalate"
        assert "but the sentence was modified" in outcome.rejected_reason

    def test_clean_keep_is_accepted_and_changes_nothing(self) -> None:
        hit = make_hit(PATIENT_SENTENCE)
        outcome = run_validate(hit, response("keep", PATIENT_SENTENCE, "patient-facing."))
        assert outcome.decision == "keep"
        assert outcome.revised_sentence == PATIENT_SENTENCE
        assert not outcome.was_rejected

    def test_parse_response_accepts_every_valid_decision(self) -> None:
        for decision in DECISIONS:
            assert parse_response(response(decision, "x"))["decision"] == decision


class TestFixtureMode:
    def test_recorded_keep_in_patient_facing_context(self, rulebook) -> None:
        judge = Judge(rulebook, live=False, fixtures=load_fixtures(FIXTURES))
        outcome = judge.judge(make_hit(PATIENT_SENTENCE))
        assert outcome.decision == "keep"
        assert "plain-language" in outcome.justification

    def test_recorded_change_in_clinical_context(self, rulebook) -> None:
        judge = Judge(rulebook, live=False, fixtures=load_fixtures(FIXTURES))
        outcome = judge.judge(make_hit(CLINICAL_SENTENCE))
        assert outcome.decision == "change"
        assert "adverse event" in outcome.revised_sentence

    def test_adversarial_fixtures_are_all_rejected(self, rulebook) -> None:
        """Every deliberately bad response must escalate, never apply."""
        fixtures = load_fixtures(FIXTURES)
        bad_keys = [k for k in fixtures if k.split("|")[-1].isupper() and k.startswith("R-002|")]
        assert len(bad_keys) >= 5

        for key in bad_keys:
            outcome = run_validate(make_hit(), fixtures[key])
            assert outcome.decision == "escalate", f"{key} was not rejected"
            assert outcome.rejected_reason, f"{key} has no rejection reason"

    def test_fixture_mode_records_provenance(self, rulebook) -> None:
        judge = Judge(rulebook, live=False, fixtures=load_fixtures(FIXTURES))
        outcome = judge.judge(make_hit(CLINICAL_SENTENCE))
        assert outcome.prompt_version == PROMPT_VERSION
        assert outcome.prompt_hash
        assert "fixture" in outcome.model

    def test_default_stand_in_keeps_patient_facing_language(self, rulebook) -> None:
        judge = Judge(rulebook, live=False, fixtures={})
        hit = make_hit("Report any side effect now.",
                       paragraph="What you may experience: report any side effect now.")
        assert judge.judge(hit).decision == "keep"

    def test_default_stand_in_changes_professional_language(self, rulebook) -> None:
        judge = Judge(rulebook, live=False, fixtures={})
        outcome = judge.judge(make_hit(CLINICAL_SENTENCE))
        assert outcome.decision == "change"
        assert "adverse event" in outcome.revised_sentence

    def test_dry_run_is_reproducible(self, rulebook) -> None:
        judge = Judge(rulebook, live=False, fixtures=load_fixtures(FIXTURES))
        first = [judge.judge(make_hit(CLINICAL_SENTENCE)).revised_sentence for _ in range(3)]
        assert len(set(first)) == 1


class TestCache:
    def test_cache_returns_the_recorded_response(self, rulebook, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path / "cache.db")
        hit = make_hit(CLINICAL_SENTENCE)
        system, _ = build_prompts(hit, rulebook.get("R-002"))
        hash_ = prompt_hash(system)
        key = ResponseCache.key(CLINICAL_SENTENCE, "R-002", hash_, "claude-opus-5")
        cache.put(key, sentence=CLINICAL_SENTENCE, rule_id="R-002", hash_=hash_,
                  model="claude-opus-5", response=response("keep", CLINICAL_SENTENCE),
                  request_id="req_abc")

        judge = Judge(rulebook, live=True, cache=cache)  # live, but the cache short-circuits it
        judge.model = "claude-opus-5"
        outcome = judge.judge(hit)
        assert outcome.cached is True
        assert outcome.decision == "keep"
        assert outcome.request_id == "req_abc"
        assert judge.stats["live_calls"] == 0
        cache.close()

    def test_cache_key_varies_with_every_input(self) -> None:
        base = ResponseCache.key("s", "R-001", "h", "m")
        assert base != ResponseCache.key("other", "R-001", "h", "m")
        assert base != ResponseCache.key("s", "R-002", "h", "m")
        assert base != ResponseCache.key("s", "R-001", "other", "m")
        assert base != ResponseCache.key("s", "R-001", "h", "other-model")

    def test_cache_survives_reopening(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.db"
        cache = ResponseCache(path)
        cache.put("k", sentence="s", rule_id="R-001", hash_="h", model="m",
                  response="{}", request_id=None)
        cache.close()
        assert ResponseCache(path).get("k") == ("{}", None)


class TestTokenWindow:
    def test_insertion_next_to_the_span_is_allowed(self) -> None:
        original = "The physician signs."
        span = (4, 13)
        assert detect_over_edit(original, "The healthcare provider signs.", span) is None

    def test_insertion_far_from_the_span_is_rejected(self) -> None:
        original = "The physician signs the form before the procedure begins today."
        span = (4, 13)
        assert detect_over_edit(
            original,
            "The healthcare provider signs the form before the procedure quietly begins today.",
            span,
        ) is not None

    def test_margin_is_configurable(self) -> None:
        original = "One two three four five six seven."
        span = (0, 3)
        assert detect_over_edit(original, "One two three four FIVE six seven.", span, margin=10) is None
        assert detect_over_edit(original, "One two three four FIVE six seven.", span, margin=1) is not None
