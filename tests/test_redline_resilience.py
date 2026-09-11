"""Layer C: what happens when a hit cannot be written.

The rule is that one bad hit must never cost the rest of the document, and must never
disappear quietly: it lands in ``skipped`` with a reason, which the pipeline records.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from termguard.redline import (
    RAW_ID_BASE,
    _RawPackage,
    annotate,
    build_comment,
    hit_for_llm_edit,
    minimal_diff,
    redline,
)
from termguard.rulebook import Rulebook, load_rulebook
from termguard.scanner import Hit, scan_bytes
from termguard.walker import Location

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTHOR = "TermGuard (rule engine)"


@pytest.fixture(scope="module")
def rulebook() -> Rulebook:
    return load_rulebook(REPO_ROOT / "data" / "rulebook.yaml")


@pytest.fixture(scope="module")
def source(corpus_dir: Path) -> bytes:
    return (corpus_dir / "IFU-001.docx").read_bytes()


def first_hit(source: bytes, rulebook: Rulebook, part: str = "body") -> Hit:
    return next(
        h for h in scan_bytes(source, "IFU-001.docx", rulebook)
        if h.classification == "unambiguous" and h.part == part
    )


class TestSkippedHits:
    def test_missing_body_paragraph_is_skipped_with_a_reason(self, source, rulebook) -> None:
        hit = first_hit(source, rulebook)
        phantom = replace(hit, location=replace(hit.location, paragraph_index=9999))
        result = redline(source, [phantom], rulebook, author=AUTHOR)
        assert result.count == 0
        assert len(result.skipped) == 1
        assert "not found by docx-editor" in result.skipped[0][1]

    def test_text_absent_from_the_paragraph_is_skipped(self, source, rulebook) -> None:
        hit = first_hit(source, rulebook)
        ghost = replace(hit, matched_text="text that is not in this document")
        result = redline(source, [ghost], rulebook, author=AUTHOR)
        assert result.count == 0
        assert result.skipped

    def test_a_bad_hit_does_not_cost_the_good_ones(self, source, rulebook) -> None:
        hits = [h for h in scan_bytes(source, "IFU-001.docx", rulebook)
                if h.classification == "unambiguous" and h.part == "body"]
        assert len(hits) > 3
        broken = replace(hits[0], location=replace(hits[0].location, paragraph_index=9999))
        result = redline(source, [broken, *hits[1:]], rulebook, author=AUTHOR)
        assert result.count == len(hits) - 1
        assert len(result.skipped) == 1

    def test_missing_part_is_skipped_with_a_reason(self, source, rulebook) -> None:
        hit = first_hit(source, rulebook, part="header")
        phantom = replace(hit, location=replace(hit.location, part_name="word/header9.xml"))
        result = redline(source, [phantom], rulebook, author=AUTHOR)
        assert result.count == 0
        assert "missing" in result.skipped[0][1]

    def test_missing_paragraph_in_a_raw_part_is_skipped(self, source, rulebook) -> None:
        hit = first_hit(source, rulebook, part="header")
        phantom = replace(hit, location=replace(hit.location, paragraph_index=42))
        result = redline(source, [phantom], rulebook, author=AUTHOR)
        assert result.count == 0
        assert "not found in word/header1.xml" in result.skipped[0][1]

    def test_stale_span_in_a_raw_part_is_recovered_by_searching(self, source, rulebook) -> None:
        """A span shifted by an earlier edit is re-derived rather than abandoned."""
        hit = first_hit(source, rulebook, part="header")
        stale = replace(hit, span=(0, len(hit.matched_text)))
        result = redline(source, [stale], rulebook, author=AUTHOR)
        assert result.count == 1
        assert result.skipped == []

    def test_text_genuinely_absent_from_a_raw_part_is_skipped(self, source, rulebook) -> None:
        hit = first_hit(source, rulebook, part="header")
        ghost = replace(hit, matched_text="nowhere to be found", span=(0, 19))
        result = redline(source, [ghost], rulebook, author=AUTHOR)
        assert result.count == 0
        assert "no longer present" in result.skipped[0][1]


class TestCommentsPart:
    def test_comments_part_is_created_when_absent(self, corpus_dir: Path) -> None:
        """A document with no comments yet must still accept one."""
        data = (corpus_dir / "CTL-001.docx").read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert "word/comments.xml" not in zf.namelist()

        package = _RawPackage(data)
        package.ensure_comments_part()
        rebuilt = package.to_bytes()

        with zipfile.ZipFile(io.BytesIO(rebuilt)) as zf:
            assert "word/comments.xml" in zf.namelist()
            assert "comments+xml" in zf.read("[Content_Types].xml").decode()
            assert "comments.xml" in zf.read("word/_rels/document.xml.rels").decode()

    def test_comment_ids_never_collide_with_docx_editor(self, corpus_dir: Path) -> None:
        package = _RawPackage((corpus_dir / "CTL-001.docx").read_bytes())
        assert package.next_comment_id() >= RAW_ID_BASE

    def test_creating_the_part_twice_is_idempotent(self, corpus_dir: Path) -> None:
        package = _RawPackage((corpus_dir / "CTL-001.docx").read_bytes())
        package.ensure_comments_part()
        package.ensure_comments_part()
        with zipfile.ZipFile(io.BytesIO(package.to_bytes())) as zf:
            types = zf.read("[Content_Types].xml").decode()
        assert types.count("/word/comments.xml") == 1


class TestLlmEditMapping:
    def _hit(self, sentence: str, matched: str, approved: str, paragraph: str | None = None) -> Hit:
        paragraph = paragraph or sentence
        start = sentence.find(matched)
        return Hit(
            location=Location(file="t.docx", part="body", part_name="word/document.xml",
                              paragraph_index=0),
            rule_id="R-002", matched_text=matched, approved_text=approved,
            span=(start, start + len(matched)), occurrence=0, sentence=sentence,
            paragraph_text=paragraph, classification="needs_judgment",
        )

    def test_unchanged_revision_yields_no_edit(self) -> None:
        hit = self._hit("Report a side effect.", "side effect", "adverse event")
        assert hit_for_llm_edit(hit, "Report a side effect.") is None

    def test_sentence_absent_from_the_paragraph_yields_no_edit(self) -> None:
        hit = self._hit("Report a side effect.", "side effect", "adverse event",
                        paragraph="A completely different paragraph.")
        assert hit_for_llm_edit(hit, "Report an adverse event.") is None

    def test_grammatical_agreement_is_carried_inside_one_change(self) -> None:
        hit = self._hit("Report a side effect promptly.", "side effect", "adverse event")
        edit = hit_for_llm_edit(hit, "Report an adverse event promptly.")
        assert edit is not None
        assert edit.matched_text == "a side effect"
        assert edit.approved_text == "an adverse event"

    def test_span_is_absolute_within_the_paragraph(self) -> None:
        paragraph = "First sentence. Report a side effect promptly."
        hit = self._hit("Report a side effect promptly.", "side effect", "adverse event",
                        paragraph=paragraph)
        edit = hit_for_llm_edit(hit, "Report an adverse event promptly.")
        assert paragraph[edit.span[0]:edit.span[1]] == "a side effect"

    def test_identical_strings_have_no_diff(self) -> None:
        assert minimal_diff("same text", "same text") is None


class TestAnnotation:
    def test_comment_only_annotation_changes_no_text(self, source, rulebook) -> None:
        hit = next(h for h in scan_bytes(source, "IFU-001.docx", rulebook) if h.needs_judgment)
        data, applied, _ = annotate(source, [(hit, "Considered and left unchanged.")])
        assert len(applied) == 1
        assert applied[0].revision_id is None

        from termguard.walker import walk_bytes

        before = [p.text for p in walk_bytes(source, "x")]
        after = [p.text for p in walk_bytes(data, "x")]
        assert before == after

    def test_non_body_annotations_are_reported_not_silently_dropped(
        self, source, rulebook
    ) -> None:
        hit = first_hit(source, rulebook, part="header")
        _, applied, skipped = annotate(source, [(hit, "note")])
        assert applied == []
        assert skipped and "outside the body" in skipped[0][1]

    def test_nothing_to_annotate_is_a_no_op(self, source) -> None:
        data, applied, skipped = annotate(source, [])
        assert data == source and applied == [] and skipped == []

    def test_missing_paragraph_is_reported(self, source, rulebook) -> None:
        hit = next(h for h in scan_bytes(source, "IFU-001.docx", rulebook) if h.needs_judgment)
        phantom = replace(hit, location=replace(hit.location, paragraph_index=9999))
        _, applied, skipped = annotate(source, [(phantom, "note")])
        assert applied == []
        assert "paragraph not found" in skipped[0][1]

    def test_absent_anchor_text_is_reported(self, source, rulebook) -> None:
        hit = next(h for h in scan_bytes(source, "IFU-001.docx", rulebook) if h.needs_judgment)
        ghost = replace(hit, matched_text="this text does not occur anywhere")
        _, applied, skipped = annotate(source, [(ghost, "note")])
        assert applied == []
        assert skipped


class TestAppliedChangeRecord:
    def test_record_exposes_rule_and_serializes(self, source, rulebook) -> None:
        hit = first_hit(source, rulebook)
        result = redline(source, [hit], rulebook, author=AUTHOR)
        change = result.applied[0]
        assert change.rule_id == hit.rule_id
        payload = change.as_dict()
        assert payload["engine"] == "docx-editor"
        assert payload["revision_ids"]
        assert payload["location"]["container_path"]

    def test_comment_text_cites_rule_and_mechanism(self, source, rulebook) -> None:
        hit = first_hit(source, rulebook)
        text = build_comment(hit, rulebook, "deterministic")
        assert text.startswith(f"{hit.rule_id}:")
        assert text.endswith("Mechanism: deterministic.")
