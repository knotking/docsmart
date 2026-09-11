"""The exportable audit trail - the artifact QA asks for.

One row per change, carrying its mechanism, its provenance and the reviewer's ruling.
"""

from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path

import pytest
from sqlmodel import select

from termguard import audit, review
from termguard.models import Change, DecisionKind, Mechanism
from termguard.pipeline import run_pipeline

SUBSET = ("IFU-001.docx", "RMS-001.docx")


@pytest.fixture
def run_with_decisions(session, store, settings, corpus_dir: Path, tmp_path: Path):
    from dataclasses import replace

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in SUBSET:
        shutil.copy(corpus_dir / name, corpus / name)

    run, _ = run_pipeline(
        session, settings=replace(settings, corpus_dir=corpus), store=store,
        dry_run=True, actor="tester",
    )
    pending = review.pending_changes(session, run.id)
    review.record_decision(session, pending[0].id, DecisionKind.REJECTED, "qa@meridian",
                           note="not appropriate here")
    review.record_decision(session, pending[1].id, DecisionKind.EDITED, "qa@meridian",
                           final_text="is required to")
    for change in pending[2:]:
        review.record_decision(session, change.id, DecisionKind.ACCEPTED, "qa@meridian")
    session.commit()
    return run


def read_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


class TestAuditCsv:
    def test_one_row_per_change(self, session, run_with_decisions) -> None:
        run = run_with_decisions
        rows = read_csv(audit.audit_csv(session, run.id))
        assert len(rows) == len(session.exec(select(Change).where(Change.run_id == run.id)).all())

    def test_mechanism_and_model_columns_are_populated(self, session, run_with_decisions) -> None:
        rows = read_csv(audit.audit_csv(session, run_with_decisions.id))
        assert {r["mechanism"] for r in rows} == {"deterministic", "ai"}
        ai_rows = [r for r in rows if r["mechanism"] == "ai"]
        assert ai_rows and all(r["model"] for r in ai_rows)
        assert all(r["prompt_hash"] for r in ai_rows)
        deterministic = [r for r in rows if r["mechanism"] == "deterministic"]
        assert all(r["model"] == "" for r in deterministic)

    def test_every_row_names_its_document_and_location(self, session, run_with_decisions) -> None:
        for row in read_csv(audit.audit_csv(session, run_with_decisions.id)):
            assert row["document"].endswith(".docx")
            assert row["part"]
            assert row["rule_id"].startswith("R-")

    def test_rows_carry_the_content_hash_of_the_version_they_landed_in(
        self, session, run_with_decisions
    ) -> None:
        for row in read_csv(audit.audit_csv(session, run_with_decisions.id)):
            assert len(row["content_sha256"]) == 64

    def test_decisions_appear_with_their_reviewer(self, session, run_with_decisions) -> None:
        rows = read_csv(audit.audit_csv(session, run_with_decisions.id))
        decisions = {r["decision"] for r in rows}
        assert {"accepted", "rejected", "edited"} <= decisions
        assert all(r["reviewer"] == "qa@meridian" for r in rows if r["decision"] != "undecided")

    def test_an_edited_row_shows_the_reviewers_wording(self, session, run_with_decisions) -> None:
        rows = read_csv(audit.audit_csv(session, run_with_decisions.id))
        edited = [r for r in rows if r["decision"] == "edited"]
        assert edited and edited[0]["final_text"] == "is required to"

    def test_a_superseded_decision_shows_the_latest(self, session, run_with_decisions) -> None:
        run = run_with_decisions
        change = session.exec(select(Change).where(Change.run_id == run.id)).first()
        review.record_decision(session, change.id, DecisionKind.REJECTED, "second@x",
                               note="overruled")
        session.commit()
        row = next(r for r in read_csv(audit.audit_csv(session, run.id))
                   if r["change_id"] == str(change.id))
        assert row["decision"] == "rejected"
        assert row["reviewer"] == "second@x"

    def test_header_matches_the_declared_columns(self, session, run_with_decisions) -> None:
        text = audit.audit_csv(session, run_with_decisions.id)
        assert text.splitlines()[0].split(",") == audit.AUDIT_COLUMNS

    def test_csv_is_parseable_with_embedded_commas_and_quotes(
        self, session, run_with_decisions
    ) -> None:
        rows = read_csv(audit.audit_csv(session, run_with_decisions.id))
        assert all(len(r) == len(audit.AUDIT_COLUMNS) for r in rows)

    def test_undecided_changes_are_shown_as_undecided(self, session, store, settings,
                                                      corpus_dir: Path, tmp_path: Path) -> None:
        from dataclasses import replace

        corpus = tmp_path / "c2"
        corpus.mkdir()
        shutil.copy(corpus_dir / "RMS-001.docx", corpus / "RMS-001.docx")
        run, _ = run_pipeline(session, settings=replace(settings, corpus_dir=corpus),
                              store=store, dry_run=True)
        rows = read_csv(audit.audit_csv(session, run.id))
        assert rows and all(r["decision"] == "undecided" for r in rows)


class TestEventLog:
    def test_events_for_a_document_span_its_whole_life(self, session, run_with_decisions) -> None:
        from termguard.models import Document

        document = session.exec(select(Document).where(Document.name == "IFU-001.docx")).one()
        events = audit.events_for_document(session, document.id)
        kinds = [e.event for e in events]
        assert kinds[0] == "version.ingested"
        assert "document.scanned" in kinds
        assert "change.proposed" in kinds
        assert "decision.recorded" in kinds

    def test_events_are_ordered_and_never_rewritten(self, session, run_with_decisions) -> None:
        events = audit.events_for_run(session, run_with_decisions.id)
        assert [e.id for e in events] == sorted(e.id for e in events)

    def test_ai_events_are_attributed_to_the_model_not_a_person(
        self, session, run_with_decisions
    ) -> None:
        from termguard.models import ActorKind

        events = [e for e in audit.events_for_run(session, run_with_decisions.id)
                  if e.mechanism is Mechanism.AI and e.event == "change.proposed"]
        assert events
        assert all(e.actor_kind is ActorKind.LLM for e in events)
        assert all("AI-proposed" in e.actor for e in events)
