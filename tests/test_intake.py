"""Layer A intake: sources in, candidate rules out, a person in between.

The dangerous failure in this layer is a *reversed* rule — one whose deprecated and
approved terms are swapped. It does not error. It rewrites correct text into wrong text
across every document on the next run. So a large share of these tests are about
direction, and about refusing to guess it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import docx
import pytest
from sqlmodel import select

from termguard import intake
from termguard.extract import PATTERNS, Candidate, extract, extract_from_tables, next_rule_id
from termguard.models import CandidateStatus, Run, RunStatus, RuleCandidate
from termguard.rulebook import CaseKind, MatchKind, load_rulebook
from termguard.sources import (
    ExtractedSource,
    SourceError,
    SourceLine,
    from_captions,
    from_html,
    read_bytes,
    read_source,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def line(text: str, kind: str = "text", locator: str = "l1") -> SourceLine:
    return SourceLine(text=text, locator=locator, kind=kind)


def source_of(*texts: str, kind: str = "text") -> ExtractedSource:
    return ExtractedSource(
        name="t", kind="document", origin="t",
        lines=[line(t, kind=kind, locator=f"l{i}") for i, t in enumerate(texts)],
    )


@pytest.fixture(scope="module")
def rulebook():
    return load_rulebook(REPO_ROOT / "data" / "rulebook.yaml")


@pytest.fixture
def style_guide(tmp_path: Path) -> Path:
    """A style guide shaped like the real thing: a glossary table plus prose."""
    path = tmp_path / "styleguide.docx"
    document = docx.Document()
    document.add_heading("Terminology Standard", level=1)

    table = document.add_table(rows=3, cols=3)
    for row, cells in enumerate([
        ("Do not use", "Use instead", "Notes"),
        ("flow rate", "delivery rate", ""),
        ("cassette", "reservoir", "Only in the hardware manual; depends on context"),
    ]):
        for column, value in enumerate(cells):
            table.cell(row, column).text = value

    document.add_paragraph("Use mL, not ml, in printed labelling.")
    document.add_paragraph("Do not use IFU; use Instructions for Use on first mention.")
    document.add_paragraph("This sentence states no rule whatsoever.")
    document.save(path)
    return path


@pytest.fixture
def rulebook_copy(tmp_path: Path) -> Path:
    path = tmp_path / "rulebook.yaml"
    shutil.copy(REPO_ROOT / "data" / "rulebook.yaml", path)
    return path


class TestSources:
    def test_docx_reassembles_table_rows(self, style_guide: Path) -> None:
        """The walker yields one paragraph per cell; a glossary needs the row."""
        source = read_source(style_guide)
        rows = [l for l in source.lines if l.kind == "table_row"]
        assert rows
        assert "Do not use | Use instead" in rows[0].text
        assert "flow rate | delivery rate" in rows[1].text

    def test_docx_keeps_prose_separate_from_tables(self, style_guide: Path) -> None:
        source = read_source(style_guide)
        prose = [l.text for l in source.lines if l.kind == "text"]
        assert any("Use mL, not ml" in t for t in prose)

    def test_every_line_carries_a_locator(self, style_guide: Path) -> None:
        assert all(l.locator for l in read_source(style_guide).lines)

    def test_content_is_hashed(self, style_guide: Path) -> None:
        assert len(read_source(style_guide).content_sha256) == 64

    def test_html_tables_survive_as_rows(self) -> None:
        markup = (
            "<table><tr><th>Deprecated</th><th>Approved</th></tr>"
            "<tr><td>side effect</td><td>adverse event</td></tr></table>"
        )
        rows = [l for l in from_html(markup, "x") if l.kind == "table_row"]
        assert rows[1].text == "side effect | adverse event"

    def test_html_scripts_are_dropped(self) -> None:
        text = " ".join(l.text for l in from_html("<script>var x='use A not B'</script><p>Real text here.</p>", "x"))
        assert "var x" not in text

    def test_captions_keep_their_timestamps(self) -> None:
        vtt = b"""WEBVTT

00:00:05.000 --> 00:00:09.000
Use adverse event, not side effect.

00:03:21.000 --> 00:03:25.000
The old name is deprecated.
"""
        lines = from_captions(vtt, "t.vtt")
        assert lines[0].locator == "00:00:05"
        assert lines[1].locator == "00:03:21"
        assert "adverse event" in lines[0].text

    def test_srt_captions_drop_the_index_lines(self) -> None:
        srt = b"1\n00:00:01,000 --> 00:00:04,000\nUse mL, not ml.\n"
        lines = from_captions(srt, "t.srt")
        assert len(lines) == 1 and "mL" in lines[0].text

    def test_an_image_says_what_it_needs(self, tmp_path: Path, monkeypatch) -> None:
        """An unreadable source must never look like one containing no terminology."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        path = tmp_path / "glossary.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        source = read_source(path)
        assert source.readable is False
        assert source.needs and "ANTHROPIC_API_KEY" in source.needs[0]

    def test_a_video_without_captions_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "training.mp4"
        path.write_bytes(b"\x00" * 64)
        source = read_source(path)
        assert source.readable is False
        assert any(".vtt" in need for need in source.needs)

    def test_a_video_with_captions_alongside_is_read(self, tmp_path: Path) -> None:
        (tmp_path / "training.mp4").write_bytes(b"\x00" * 64)
        (tmp_path / "training.vtt").write_bytes(
            b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nUse mL, not ml.\n"
        )
        source = read_source(tmp_path / "training.mp4")
        assert source.readable is True
        assert source.lines[0].locator == "00:00:10"

    def test_an_unsupported_type_is_refused_by_name(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.xyz"
        path.write_bytes(b"x")
        with pytest.raises(SourceError, match="unsupported source type"):
            read_source(path)

    def test_uploads_are_read_from_memory(self, style_guide: Path) -> None:
        source = read_bytes(style_guide.read_bytes(), "styleguide.docx")
        assert source.readable and source.origin == "styleguide.docx"


class TestDirection:
    """The reversal hazard. Every one of these is about which term is which."""

    @pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.name)
    def test_each_pattern_reads_its_own_example_correctly(self, pattern) -> None:
        found = extract(source_of(pattern.example))
        match = next((c for c in found if c.method == pattern.name), None)
        assert match is not None, f"{pattern.name} did not match its own example"
        # Confirm the direction by construction: the deprecated term must be the one the
        # sentence tells you to stop using.
        assert match.deprecated and match.approved
        assert match.deprecated != match.approved

    def test_use_x_not_y_puts_y_on_the_deprecated_side(self) -> None:
        found = extract(source_of("Use mL, not ml."))
        assert (found[0].deprecated, found[0].approved) == ("ml", "mL")

    def test_the_mirrored_sentence_gives_the_mirrored_rule(self) -> None:
        """'Replace X with Y' and 'Use Y, not X' must produce the same rule."""
        a = extract(source_of("Replace side effect with adverse event."))[0]
        b = extract(source_of("Use adverse event, not side effect."))[0]
        assert (a.deprecated, a.approved) == (b.deprecated, b.approved)

    def test_a_table_direction_comes_from_its_headings(self) -> None:
        source = source_of(
            "Deprecated | Approved", "side effect | adverse event", kind="table_row"
        )
        found = extract_from_tables(source)
        assert (found[0].deprecated, found[0].approved) == ("side effect", "adverse event")

    def test_reversed_headings_reverse_the_rule(self) -> None:
        source = source_of(
            "Approved | Deprecated", "adverse event | side effect", kind="table_row"
        )
        found = extract_from_tables(source)
        assert (found[0].deprecated, found[0].approved) == ("side effect", "adverse event")

    def test_a_table_with_unrecognised_headings_yields_nothing(self) -> None:
        """Column order is not a fallback: guessing direction is what reverses rules."""
        source = source_of("Column A | Column B", "alpha | beta", kind="table_row")
        assert extract_from_tables(source) == []

    def test_a_table_with_no_heading_row_yields_nothing(self) -> None:
        source = source_of("alpha | beta", "gamma | delta", kind="table_row")
        assert extract_from_tables(source) == []


class TestOverCapture:
    """A term that swallows its sentence matches nothing, so the rule silently does
    nothing — worse than an obviously broken one."""

    @pytest.mark.parametrize(
        "sentence,deprecated,approved",
        [
            ("Do not use IFU; use Instructions for Use on first mention.",
             "IFU", "Instructions for Use"),
            ("The term Meridian Pump 2 is deprecated; use Meridian Infusion System.",
             "Meridian Pump 2", "Meridian Infusion System"),
            ("Replace drug library with medication library throughout.",
             "drug library", "medication library"),
            ("Prefer must over shall in requirement statements.", "shall", "must"),
        ],
    )
    def test_terms_stop_at_the_end_of_the_term(self, sentence, deprecated, approved) -> None:
        found = extract(source_of(sentence))
        assert found, f"nothing extracted from {sentence!r}"
        assert (found[0].deprecated, found[0].approved) == (deprecated, approved)

    def test_prepositions_inside_a_term_survive(self) -> None:
        """'Instructions for Use' contains 'for' and must not be truncated to it."""
        found = extract(source_of("Do not use IFU; use Instructions for Use."))
        assert found[0].approved == "Instructions for Use"

    def test_a_sentence_stating_no_rule_yields_nothing(self) -> None:
        assert extract(source_of("This document describes safe operation of the device.")) == []


class TestConflictDetection:
    def test_a_duplicate_of_an_existing_rule_is_flagged(self, rulebook) -> None:
        found = extract(source_of("Use clinician, not nurse."), rulebook=rulebook)
        assert any("already covered by R-010" in w for w in found[0].warnings)

    def test_a_contradiction_is_flagged_as_a_conflict(self, rulebook) -> None:
        """The rulebook says physician -> healthcare provider; this source disagrees."""
        found = extract(source_of("Use doctor, not physician."), rulebook=rulebook)
        assert any(w.startswith("CONFLICT") for w in found[0].warnings)

    def test_a_case_only_rule_is_not_reported_as_conflicting_with_itself(self, rulebook) -> None:
        """'ml' and 'mL' are equal under casefold; a careless check flags R-007 twice."""
        found = extract(source_of("Use mL, not ml."), rulebook=rulebook)
        conflicts = [w for w in found[0].warnings if w.startswith("CONFLICT")]
        assert conflicts == []

    def test_a_novel_term_has_no_warnings(self, rulebook) -> None:
        found = extract(source_of("Use delivery rate, not flow rate."), rulebook=rulebook)
        assert found[0].warnings == []

    def test_repeats_are_collapsed_and_counted(self) -> None:
        found = extract(source_of(
            "Use mL, not ml.", "Use mL, not ml.", "Replace ml with mL."
        ))
        assert len(found) == 1
        assert "stated" in found[0].note


class TestContextDetection:
    def test_a_note_saying_it_depends_flags_the_rule_for_judgment(self) -> None:
        source = source_of(
            "Do not use | Use instead | Notes",
            "cassette | reservoir | Only in the hardware manual; depends on context",
            kind="table_row",
        )
        found = extract_from_tables(source)
        assert found[0].suggested["context_required"] is True
        assert found[0].suggested["context_note"]

    def test_a_plain_note_does_not(self) -> None:
        source = source_of(
            "Deprecated | Approved | Notes", "flow rate | delivery rate | Renamed in 2024",
            kind="table_row",
        )
        assert extract_from_tables(source)[0].suggested == {}


class TestRuleConstruction:
    def test_a_multiword_term_becomes_a_phrase_rule(self) -> None:
        candidate = Candidate(deprecated="flow rate", approved="delivery rate",
                              quote="q", locator="l", source_name="s", method="m")
        assert candidate.to_rule("R-050").match is MatchKind.PHRASE

    def test_a_single_word_becomes_whole_word(self) -> None:
        candidate = Candidate(deprecated="nurse", approved="clinician",
                              quote="q", locator="l", source_name="s", method="m")
        assert candidate.to_rule("R-050").match is MatchKind.WHOLE_WORD

    def test_a_case_only_rule_replaces_verbatim(self) -> None:
        """Preserving the source's casing would reproduce the very spelling being fixed."""
        candidate = Candidate(deprecated="ml", approved="mL",
                              quote="q", locator="l", source_name="s", method="m")
        assert candidate.to_rule("R-050").case is CaseKind.EXACT

    def test_the_rationale_cites_the_source(self) -> None:
        candidate = Candidate(deprecated="a", approved="b", quote="q",
                              locator="page 4", source_name="guide.docx", method="m")
        assert "guide.docx" in candidate.to_rule("R-050").rationale

    def test_next_id_skips_taken_ones(self, rulebook) -> None:
        assert next_rule_id(rulebook) == "R-013"


class TestIntake:
    def test_ingesting_persists_source_and_candidates(
        self, session, store, style_guide, rulebook
    ) -> None:
        source, candidates = intake.ingest_file(
            session, style_guide.read_bytes(), "styleguide.docx",
            uploaded_by="qa", rulebook=rulebook, store=store,
        )
        assert source.lines_read > 0
        assert source.candidates_found == len(candidates) > 0
        assert source.blob_uri  # the source itself is kept, content-addressed
        assert len(intake.pending(session)) == len(candidates)

    def test_an_unreadable_source_is_still_recorded(self, session, tmp_path: Path) -> None:
        path = tmp_path / "g.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        source, candidates = intake.ingest_file(session, path.read_bytes(), "g.png")
        assert candidates == []
        assert source.needs
        assert intake.describe_source(session, source)["readable"] is False

    def test_accepting_writes_a_rule_and_moves_the_hash(
        self, session, style_guide, rulebook_copy
    ) -> None:
        before = load_rulebook(rulebook_copy)
        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx", rulebook=before)
        candidate = next(c for c in intake.pending(session) if c.deprecated == "flow rate")

        decided, after = intake.accept(session, candidate.id, "qa",
                                       rulebook_path=rulebook_copy, owner="systems-engineering")
        assert decided.status is CandidateStatus.ACCEPTED
        assert len(after) == len(before) + 1
        assert after.hash != before.hash
        assert after.get(decided.rule_id).owner == "systems-engineering"

    def test_accepting_with_corrections_is_recorded_as_edited(
        self, session, style_guide, rulebook_copy
    ) -> None:
        """'a person kept what the machine proposed' and 'a person rewrote it' differ."""
        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx",
                           rulebook=load_rulebook(rulebook_copy))
        candidate = next(c for c in intake.pending(session) if c.deprecated == "cassette")

        decided, after = intake.accept(
            session, candidate.id, "qa", rulebook_path=rulebook_copy,
            overrides={"approved": "reservoir cassette", "scope": ["body", "tables"]},
        )
        assert decided.status is CandidateStatus.EDITED
        rule = after.get(decided.rule_id)
        assert rule.approved == "reservoir cassette"
        assert [s.value for s in rule.scope] == ["body", "tables"]

    def test_overrides_are_validated_not_trusted(
        self, session, style_guide, rulebook_copy
    ) -> None:
        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx",
                           rulebook=load_rulebook(rulebook_copy))
        candidate = intake.pending(session)[0]
        with pytest.raises(intake.IntakeError, match="invalid rule"):
            intake.accept(session, candidate.id, "qa", rulebook_path=rulebook_copy,
                          overrides={"match": "nonsense"})

    def test_a_rule_the_rulebook_rejects_leaves_the_file_intact(
        self, session, style_guide, rulebook_copy
    ) -> None:
        """A failed write must not leave a broken rulebook behind."""
        before = load_rulebook(rulebook_copy)
        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx", rulebook=before)
        candidate = intake.pending(session)[0]
        with pytest.raises(intake.IntakeError):
            intake.accept(session, candidate.id, "qa", rulebook_path=rulebook_copy,
                          overrides={"deprecated": ["physician"]})  # already owned by R-004
        assert load_rulebook(rulebook_copy).hash == before.hash

    def test_rejecting_keeps_the_row(self, session, style_guide, rulebook) -> None:
        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx", rulebook=rulebook)
        candidate = intake.pending(session)[0]
        intake.reject(session, candidate.id, "qa", note="not for us")

        assert intake.pending(session) and candidate.id not in {
            c.id for c in intake.pending(session)
        }
        stored = session.get(RuleCandidate, candidate.id)
        assert stored.status is CandidateStatus.REJECTED
        assert stored.decision_note == "not for us"

    def test_a_decided_candidate_cannot_be_decided_twice(
        self, session, style_guide, rulebook
    ) -> None:
        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx", rulebook=rulebook)
        candidate = intake.pending(session)[0]
        intake.reject(session, candidate.id, "qa")
        with pytest.raises(intake.IntakeError, match="already"):
            intake.reject(session, candidate.id, "qa")

    def test_accepting_flags_runs_that_now_need_redoing(
        self, session, style_guide, rulebook_copy
    ) -> None:
        """Adding a rule changes what 'correct' means; existing runs no longer describe it."""
        before = load_rulebook(rulebook_copy)
        session.add(Run(rulebook_hash=before.hash, corpus_hash="x",
                        status=RunStatus.VERIFIED))
        session.flush()

        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx", rulebook=before)
        candidate = next(c for c in intake.pending(session) if c.deprecated == "flow rate")
        _, after = intake.accept(session, candidate.id, "qa", rulebook_path=rulebook_copy)

        stale = intake.pending_reruns(session, after)
        assert len(stale) == 1
        assert stale[0]["ran_under"] == before.hash
        assert stale[0]["current"] == after.hash

    def test_ingestion_is_audited_with_its_yield(self, session, style_guide, rulebook) -> None:
        from termguard import audit
        from termguard.models import AuditEvent

        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx",
                           uploaded_by="qa@meridian", rulebook=rulebook)
        events = session.exec(
            select(AuditEvent).where(AuditEvent.event == "source.ingested")
        ).all()
        assert events and events[-1].actor == "qa@meridian"
        assert events[-1].payload["candidates"] > 0

    def test_acceptance_is_audited_with_the_quote(
        self, session, style_guide, rulebook_copy
    ) -> None:
        from termguard.models import AuditEvent

        intake.ingest_file(session, style_guide.read_bytes(), "sg.docx",
                           rulebook=load_rulebook(rulebook_copy))
        candidate = next(c for c in intake.pending(session) if c.deprecated == "flow rate")
        intake.accept(session, candidate.id, "qa", rulebook_path=rulebook_copy)

        event = session.exec(
            select(AuditEvent).where(AuditEvent.event == "candidate.accepted")
        ).all()[-1]
        assert event.payload["quote"]
        assert event.payload["previous_hash"] != event.payload["rulebook_hash"]
