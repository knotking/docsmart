"""Layer B part 2: scanner precision, recall and classification.

These are the numbers quoted in the demo, so they are measured against the planted ground
truth rather than asserted by eye.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from termguard.rulebook import Rule, Rulebook, load_rulebook
from termguard.scanner import (
    Hit,
    ScanReport,
    build_report,
    scan_document,
    scan_paragraph,
    sentence_around,
)
from termguard.walker import Location, WalkedParagraph, walk

REPO_ROOT = Path(__file__).resolve().parent.parent

ACTION_TO_CLASSIFICATION = {"change": "unambiguous", "judge": "needs_judgment"}


@pytest.fixture(scope="module")
def rulebook() -> Rulebook:
    return load_rulebook(REPO_ROOT / "data" / "rulebook.yaml")


@pytest.fixture(scope="module")
def ground_truth(corpus_dir: Path) -> dict:
    return json.loads((corpus_dir / "ground_truth.json").read_text())


@pytest.fixture(scope="module")
def report(corpus_dir: Path, rulebook: Rulebook) -> ScanReport:
    return build_report(corpus_dir, rulebook)


def key_of_hit(hit: Hit) -> tuple:
    loc = hit.location
    return (loc.file, loc.part, loc.paragraph_index, hit.rule_id, hit.matched_text.casefold())


def key_of_item(item: dict) -> tuple:
    return (
        item["file"], item["part"], item["paragraph_index"], item["rule_id"],
        item["text"].casefold(),
    )


class TestAccuracy:
    """The acceptance criteria for this layer."""

    def test_recall_is_total_for_change_and_judge_items(self, report, ground_truth) -> None:
        expected = Counter(
            key_of_item(i) for i in ground_truth["items"] if i["expected_action"] in ("change", "judge")
        )
        found = Counter(key_of_hit(h) for h in report.hits)

        missed = {k: n for k, n in expected.items() if found[k] < n}
        recall = 1 - (sum(missed.values()) / sum(expected.values()))
        assert recall == 1.0, (
            f"recall {recall:.4f}; {len(missed)} distinct locations missed:\n  "
            + "\n  ".join(f"{k} (expected {n}, found {found[k]})" for k, n in list(missed.items())[:15])
        )

    def test_no_exception_protected_item_is_ever_flagged(self, report, ground_truth) -> None:
        """A 'keep' item is quoted CFR text or a historical name. Flagging one is a defect."""
        protected = {key_of_item(i) for i in ground_truth["items"] if i["expected_action"] == "keep"}
        flagged = protected & {key_of_hit(h) for h in report.hits}
        assert not flagged, f"{len(flagged)} protected spans were flagged: {sorted(flagged)[:10]}"

    def test_precision_at_least_95_percent(self, report, ground_truth) -> None:
        planted = Counter(
            key_of_item(i) for i in ground_truth["items"] if i["expected_action"] in ("change", "judge")
        )
        found = Counter(key_of_hit(h) for h in report.hits)

        true_positives = sum(min(n, planted[k]) for k, n in found.items())
        precision = true_positives / sum(found.values())
        spurious = {k: n for k, n in found.items() if n > planted[k]}
        assert precision >= 0.95, (
            f"precision {precision:.4f}; unplanted hits:\n  "
            + "\n  ".join(f"{k} (found {n}, planted {planted[k]})" for k, n in list(spurious.items())[:15])
        )

    def test_classification_matches_the_planted_intent(self, report, ground_truth) -> None:
        """A 'change' item must be automatable; a 'judge' item must reach a human."""
        intent = {
            key_of_item(i): ACTION_TO_CLASSIFICATION[i["expected_action"]]
            for i in ground_truth["items"]
            if i["expected_action"] in ACTION_TO_CLASSIFICATION
        }
        wrong = [
            (key_of_hit(h), h.classification, intent[key_of_hit(h)], h.reason)
            for h in report.hits
            if key_of_hit(h) in intent and h.classification != intent[key_of_hit(h)]
        ]
        assert not wrong, (
            f"{len(wrong)} hits classified against intent:\n  "
            + "\n  ".join(f"{k}: got {got}, want {want} ({why})" for k, got, want, why in wrong[:15])
        )

    def test_controls_produce_no_hits_at_all(self, report) -> None:
        control_hits = [h for h in report.hits if h.file.startswith("CTL-")]
        assert not control_hits, f"clean controls flagged: {[h.matched_text for h in control_hits]}"


class TestReport:
    def test_counts_are_internally_consistent(self, report) -> None:
        assert report.unambiguous + report.needs_judgment == report.total
        assert sum(report.by_file().values()) == report.total
        assert sum(report.by_part().values()) == report.total
        assert sum(report.by_rule().values()) == report.total

    def test_non_body_parts_are_counted_separately(self, report) -> None:
        """'Here are the 5 in headers your team would have missed' is the demo line."""
        by_part = report.by_part()
        assert by_part.get("header", 0) > 0
        assert by_part.get("footer", 0) > 0
        assert by_part.get("footnote", 0) > 0

    def test_serializes_round_trip(self, report) -> None:
        payload = json.loads(json.dumps(report.to_dict()))
        assert payload["total"] == report.total
        assert len(payload["hits"]) == report.total
        assert payload["hits"][0]["location"]["container_path"]

    def test_every_rule_in_the_book_fires_somewhere(self, report, rulebook) -> None:
        assert set(report.by_rule()) == {r.id for r in rulebook}


class TestExceptions:
    def _scan_text(self, text: str, rule: Rule, **loc_kwargs) -> list[Hit]:
        location = Location(file="t.docx", part="body", part_name="word/document.xml",
                            paragraph_index=0, **loc_kwargs)
        para = WalkedParagraph(location=location, text=text, element=None, run_map=[])
        return scan_paragraph(para, Rulebook(rules=[rule]))

    def test_quoted_citation_protects_every_term_inside_it(self) -> None:
        rule = Rule(id="R-004", deprecated=["physician"], approved="healthcare provider",
                    exceptions=['"[^"]*physician[^"]*"'])
        hits = self._scan_text(
            'The rule says "the physician and the physician assistant agree" here.', rule
        )
        assert hits == []

    def test_the_same_term_outside_the_quote_is_still_flagged(self) -> None:
        rule = Rule(id="R-004", deprecated=["physician"], approved="healthcare provider",
                    exceptions=['"[^"]*physician[^"]*"'])
        hits = self._scan_text('A physician reads "the physician shall sign" aloud.', rule)
        assert len(hits) == 1
        assert hits[0].span[0] < hits[0].paragraph_text.index('"')

    def test_historical_reference_is_protected(self, rulebook) -> None:
        location = Location(file="t.docx", part="body", part_name="word/document.xml",
                            paragraph_index=0)
        para = WalkedParagraph(
            location=location,
            text="This supersedes the device formerly known as the Meridian Pump 2, retained.",
            element=None, run_map=[],
        )
        assert [h for h in scan_paragraph(para, rulebook) if h.rule_id == "R-001"] == []

    def test_document_number_is_protected_but_prose_use_is_not(self, rulebook) -> None:
        location = Location(file="t.docx", part="body", part_name="word/document.xml",
                            paragraph_index=0)
        para = WalkedParagraph(
            location=location, text="Document IFU-004 explains it; refer to the IFU first.",
            element=None, run_map=[],
        )
        hits = [h for h in scan_paragraph(para, rulebook) if h.rule_id == "R-003"]
        assert len(hits) == 1
        assert hits[0].span[0] > para.text.index("refer")


class TestClassification:
    def _classify(self, text: str, rule: Rule, **loc_kwargs) -> Hit:
        location = Location(file="t.docx", part="body", part_name="word/document.xml",
                            paragraph_index=0, **loc_kwargs)
        para = WalkedParagraph(location=location, text=text, element=None, run_map=[])
        hits = scan_paragraph(para, Rulebook(rules=[rule]))
        assert len(hits) == 1, f"expected exactly one hit, got {len(hits)}"
        return hits[0]

    def test_plain_swap_is_unambiguous(self) -> None:
        rule = Rule(id="R-005", deprecated=["shall"], approved="must")
        assert self._classify("The user shall respond.", rule).classification == "unambiguous"

    def test_context_required_always_needs_judgment(self) -> None:
        rule = Rule(id="R-002", deprecated=["side effect"], approved="adverse event",
                    match="phrase", context_required=True, context_note="patient-facing")
        hit = self._classify("Any side effect matters.", rule)
        assert hit.classification == "needs_judgment"
        assert "context_required" in hit.reason

    def test_article_disagreement_needs_judgment(self) -> None:
        """'a side effect' -> 'an adverse event': the article no longer agrees."""
        rule = Rule(id="R-002", deprecated=["side effect"], approved="adverse event",
                    match="phrase")
        hit = self._classify("Report a side effect promptly.", rule)
        assert hit.classification == "needs_judgment"
        assert "article" in hit.reason

    def test_matching_article_stays_unambiguous(self) -> None:
        rule = Rule(id="R-009", deprecated=["infusion set"], approved="administration set",
                    match="phrase")
        assert self._classify("Attach a infusion set now.", rule).classification == "unambiguous"

    def test_plural_disagreement_needs_judgment(self) -> None:
        rule = Rule(id="R-002", deprecated=["side effects"], approved="adverse event",
                    match="phrase")
        hit = self._classify("Common side effects include redness.", rule)
        assert hit.classification == "needs_judgment"
        assert "number disagrees" in hit.reason

    def test_possessive_needs_judgment(self) -> None:
        rule = Rule(id="R-004", deprecated=["physician"], approved="healthcare provider")
        hit = self._classify("The physician's record is filed.", rule)
        assert hit.classification == "needs_judgment"
        assert "possessive" in hit.reason

    def test_all_caps_heading_needs_judgment(self) -> None:
        rule = Rule(id="R-005", deprecated=["shall"], approved="must")
        hit = self._classify("THE USER SHALL COMPLY", rule, is_heading=True)
        assert hit.classification == "needs_judgment"
        assert "all-caps heading" in hit.reason

    def test_title_case_heading_with_differing_word_count_needs_judgment(self) -> None:
        """'Single Use' (two words) -> 'single-use' (one): Title Case cannot map cleanly."""
        rule = Rule(id="R-008", deprecated=["single use"], approved="single-use",
                    match="phrase")
        hit = self._classify("Single Use Device Policy", rule, is_heading=True)
        assert hit.classification == "needs_judgment"
        assert "Title Case" in hit.reason

    def test_title_case_heading_with_equal_word_count_is_unambiguous(self) -> None:
        rule = Rule(id="R-009", deprecated=["Infusion Set"], approved="administration set",
                    match="phrase")
        hit = self._classify("Setting Up The Infusion Set", rule, is_heading=True)
        assert hit.classification == "unambiguous"
        assert hit.approved_text == "Administration Set"

    def test_two_rules_on_one_span_need_judgment(self) -> None:
        """Overlap is the rulebook author's ambiguity, not the scanner's; a human resolves it."""
        book = Rulebook(rules=[
            Rule(id="R-001", deprecated=["infusion set"], approved="administration set",
                 match="phrase"),
            Rule(id="R-002", deprecated=["set"], approved="assembly"),
        ])
        location = Location(file="t.docx", part="body", part_name="word/document.xml",
                            paragraph_index=0)
        para = WalkedParagraph(location=location, text="Attach the infusion set now.",
                               element=None, run_map=[])
        hits = scan_paragraph(para, book)
        assert len(hits) == 2
        assert all(h.needs_judgment for h in hits)
        assert all("same span" in h.reason for h in hits)


class TestScopeAndSentence:
    def test_out_of_scope_part_produces_no_hit(self) -> None:
        rule = Rule(id="R-005", deprecated=["shall"], approved="must", scope=["body"])
        location = Location(file="t.docx", part="header", part_name="word/header1.xml",
                            paragraph_index=0)
        para = WalkedParagraph(location=location, text="The user shall comply.",
                               element=None, run_map=[])
        assert scan_paragraph(para, Rulebook(rules=[rule])) == []

    def test_sentence_extraction_isolates_the_right_sentence(self) -> None:
        text = "First sentence here. The physician signs it. Third one follows."
        assert sentence_around(text, text.index("physician"), text.index("physician") + 9) == (
            "The physician signs it."
        )

    def test_sentence_extraction_survives_decimal_points(self) -> None:
        text = "Per 21 CFR 820.180 the record is kept. A physician reviews it."
        got = sentence_around(text, text.index("record"), text.index("record") + 6)
        assert got == "Per 21 CFR 820.180 the record is kept."

    def test_sentence_falls_back_to_paragraph_when_unsplittable(self) -> None:
        text = "no terminator here"
        assert sentence_around(text, 0, 2) == text
