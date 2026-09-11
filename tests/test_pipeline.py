"""Layers D: end-to-end pipeline, review decisions and the verification gate.

These run over a three-document subset of the corpus so the whole pipeline is exercised
(body, table, heading, header, footer, footnote, deterministic and AI mechanisms) without
the cost of all 26 files.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import pytest
from sqlmodel import select

from termguard import documents, review, verify
from termguard.config import Settings
from termguard.models import (
    Change,
    ChangeStatus,
    Decision,
    DecisionKind,
    Document,
    DocumentVersion,
    Mechanism,
    Run,
    RunStatus,
    Stage,
)
from termguard.pipeline import run_pipeline
from termguard.rulebook import load_rulebook
from termguard.scanner import scan_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBSET = ("IFU-001.docx", "RMS-001.docx", "CTL-001.docx")


@pytest.fixture(scope="module")
def rulebook():
    return load_rulebook(REPO_ROOT / "data" / "rulebook.yaml")


@pytest.fixture
def small_corpus(tmp_path_factory, corpus_dir: Path) -> Path:
    target = tmp_path_factory.mktemp("corpus")
    for name in SUBSET:
        shutil.copy(corpus_dir / name, target / name)
    return target


@pytest.fixture
def pipeline_settings(settings: Settings, small_corpus: Path) -> Settings:
    from dataclasses import replace

    return replace(settings, corpus_dir=small_corpus)


@pytest.fixture
def completed_run(session, store, pipeline_settings):
    run, outcomes = run_pipeline(
        session, settings=pipeline_settings, store=store, dry_run=True, actor="tester"
    )
    return run, outcomes


class TestPipeline:
    def test_run_completes_and_records_provenance(self, completed_run, rulebook) -> None:
        run, _ = completed_run
        assert run.status is RunStatus.COMPLETE
        assert run.rulebook_hash == rulebook.hash
        assert run.corpus_hash
        assert run.finished_at is not None

    def test_every_document_is_ingested_as_v1(self, session, completed_run) -> None:
        docs = session.exec(select(Document)).all()
        assert {d.name for d in docs} == set(SUBSET)
        for document in docs:
            history = documents.history(session, document.id)
            assert history[0].version_no == 1
            assert history[0].stage is Stage.INGESTED

    def test_ingested_version_is_byte_identical_to_the_source(
        self, session, store, completed_run, small_corpus: Path
    ) -> None:
        for name in SUBSET:
            document = session.exec(select(Document).where(Document.name == name)).one()
            first = documents.history(session, document.id)[0]
            assert documents.content(store, first) == (small_corpus / name).read_bytes()

    def test_documents_with_hits_get_a_redlined_version(self, session, completed_run) -> None:
        for name in ("IFU-001.docx", "RMS-001.docx"):
            document = session.exec(select(Document).where(Document.name == name)).one()
            redlined = documents.latest_at_stage(session, document.id, Stage.REDLINED)
            assert redlined is not None
            assert redlined.parent_version_id is not None
            assert redlined.summary["deterministic"] > 0

    def test_clean_control_gets_no_redlined_version(self, session, completed_run) -> None:
        control = session.exec(select(Document).where(Document.name == "CTL-001.docx")).one()
        assert documents.latest_at_stage(session, control.id, Stage.REDLINED) is None
        assert len(documents.history(session, control.id)) == 1

    def test_mechanisms_are_counted_separately(self, session, completed_run) -> None:
        """Constraint 2: 'how much did the AI decide' must always be answerable."""
        changes = session.exec(select(Change)).all()
        deterministic = [c for c in changes if c.mechanism is Mechanism.DETERMINISTIC]
        ai = [c for c in changes if c.mechanism is Mechanism.AI]
        assert deterministic and ai
        assert all(c.model is None for c in deterministic)
        assert all(c.model for c in ai)

    def test_ai_changes_carry_full_provenance(self, session, completed_run) -> None:
        ai_changes = session.exec(
            select(Change).where(Change.mechanism == Mechanism.AI)
        ).all()
        for change in ai_changes:
            assert change.model
            assert change.prompt_hash
            assert change.prompt_version
            assert change.llm_decision in {"change", "keep", "escalate"}

    def test_deterministic_changes_never_claim_a_model(self, session, completed_run) -> None:
        for change in session.exec(
            select(Change).where(Change.mechanism == Mechanism.DETERMINISTIC)
        ).all():
            assert change.model is None and change.prompt_hash is None

    def test_nothing_is_skipped(self, completed_run) -> None:
        _, outcomes = completed_run
        assert [o.skipped for o in outcomes if o.skipped] == []

    def test_redlined_output_removes_every_unambiguous_hit(
        self, session, store, completed_run, rulebook
    ) -> None:
        document = session.exec(select(Document).where(Document.name == "IFU-001.docx")).one()
        redlined = documents.latest_at_stage(session, document.id, Stage.REDLINED)
        remaining = scan_bytes(documents.content(store, redlined), "IFU-001.docx", rulebook)
        assert [h for h in remaining if h.classification == "unambiguous"] == []

    def test_audit_trail_covers_the_whole_run(self, session, completed_run) -> None:
        from termguard import audit

        run, _ = completed_run
        events = audit.events_for_run(session, run.id)
        kinds = {e.event for e in events}
        assert {"run.started", "version.ingested", "document.scanned",
                "version.redlined", "change.proposed", "run.completed"} <= kinds

    def test_progress_events_are_emitted(self, session, store, pipeline_settings) -> None:
        seen: list[str] = []
        run_pipeline(session, settings=pipeline_settings, store=store,
                     progress=lambda event, _payload: seen.append(event))
        assert "run.started" in seen and "run.completed" in seen
        assert seen.count("document.ingested") == len(SUBSET)


class TestReviewDecisions:
    def test_queue_holds_every_change_until_decided(self, session, completed_run) -> None:
        run, _ = completed_run
        pending = review.pending_changes(session, run.id)
        assert len(pending) == len(session.exec(select(Change)).all())

    def test_deciding_removes_an_item_from_the_queue(self, session, completed_run) -> None:
        run, _ = completed_run
        first = review.pending_changes(session, run.id)[0]
        review.record_decision(session, first.id, DecisionKind.ACCEPTED, "reviewer@x")
        assert first.id not in {c.id for c in review.pending_changes(session, run.id)}

    def test_decisions_are_append_only(self, session, completed_run) -> None:
        """Changing your mind inserts a row; the earlier decision remains as history."""
        run, _ = completed_run
        change = review.pending_changes(session, run.id)[0]
        review.record_decision(session, change.id, DecisionKind.ACCEPTED, "first@x")
        review.record_decision(session, change.id, DecisionKind.REJECTED, "second@x",
                               note="changed my mind")

        rows = session.exec(
            select(Decision).where(Decision.change_id == change.id).order_by(Decision.id)
        ).all()
        assert [r.decision for r in rows] == [DecisionKind.ACCEPTED, DecisionKind.REJECTED]

        from termguard import audit

        assert audit.latest_decision(session, change.id).reviewer == "second@x"

    def test_edited_decision_requires_final_text(self, session, completed_run) -> None:
        run, _ = completed_run
        change = review.pending_changes(session, run.id)[0]
        with pytest.raises(ValueError, match="requires final_text"):
            review.record_decision(session, change.id, DecisionKind.EDITED, "r@x")

    def test_each_decision_is_audited_with_its_reviewer(self, session, completed_run) -> None:
        from termguard import audit

        run, _ = completed_run
        change = review.pending_changes(session, run.id)[0]
        review.record_decision(session, change.id, DecisionKind.ACCEPTED, "qa@meridian")
        events = [e for e in audit.events_for_run(session, run.id)
                  if e.event == "decision.recorded"]
        assert events and events[-1].actor == "qa@meridian"

    def test_queue_item_carries_everything_a_reviewer_needs(self, session, completed_run) -> None:
        run, _ = completed_run
        ai_change = session.exec(
            select(Change).where(Change.mechanism == Mechanism.AI)
        ).first()
        item = review.queue_item(session, ai_change)
        assert item["rule_id"] and item["sentence"] and item["mechanism"] == "ai"
        assert item["model"] and item["justification"]


class TestVerificationGate:
    def _accept_all(self, session, run) -> int:
        return review.auto_accept_all(session, run.id, reviewer="qa@meridian")

    def test_undecided_changes_block_verification(self, session, store, completed_run,
                                                  pipeline_settings, rulebook) -> None:
        run, _ = completed_run
        report = verify.verify_run(session, run.id, settings=pipeline_settings,
                                   store=store, rulebook=rulebook, write_outputs=False)
        assert not report.passed
        assert report.totals["undecided"] > 0

    def test_accepting_everything_passes_the_gate(self, session, store, completed_run,
                                                  pipeline_settings, rulebook) -> None:
        run, _ = completed_run
        self._accept_all(session, run)
        report = verify.verify_run(session, run.id, settings=pipeline_settings,
                                   store=store, rulebook=rulebook, write_outputs=False)
        assert report.passed, [f.failure_reasons for f in report.files if not f.passed]
        assert report.totals["remaining_hits"] == 0
        assert report.totals["unexplained_edits"] == 0

    def test_ratified_keeps_are_reported_as_exceptions_not_failures(
        self, session, store, completed_run, pipeline_settings, rulebook
    ) -> None:
        """A term a reviewer kept is still found by the scanner - and that is correct."""
        run, _ = completed_run
        self._accept_all(session, run)
        report = verify.verify_run(session, run.id, settings=pipeline_settings,
                                   store=store, rulebook=rulebook, write_outputs=False)
        assert report.passed
        assert report.totals["adjudicated_exceptions"] > 0
        assert report.totals["kept"] > 0

    def test_final_version_is_recorded_in_the_chain(self, session, store, completed_run,
                                                    pipeline_settings, rulebook) -> None:
        run, _ = completed_run
        self._accept_all(session, run)
        verify.verify_run(session, run.id, settings=pipeline_settings, store=store,
                          rulebook=rulebook, write_outputs=False)

        document = session.exec(select(Document).where(Document.name == "IFU-001.docx")).one()
        stages = [v.stage for v in documents.history(session, document.id)]
        assert stages == [Stage.INGESTED, Stage.REDLINED, Stage.VERIFIED]

    def test_final_text_carries_the_approved_terms(self, session, store, completed_run,
                                                   pipeline_settings, rulebook) -> None:
        run, _ = completed_run
        self._accept_all(session, run)
        verify.verify_run(session, run.id, settings=pipeline_settings, store=store,
                          rulebook=rulebook, write_outputs=False)

        document = session.exec(select(Document).where(Document.name == "IFU-001.docx")).one()
        final = documents.latest(session, document.id)
        from termguard.walker import walk_bytes

        text = " ".join(p.text for p in walk_bytes(documents.content(store, final), "x"))
        assert "Meridian Infusion System" in text
        assert "Meridian Pump 2" not in text
        assert "administration set" in text

    def test_final_document_has_no_unresolved_revisions(self, session, store, completed_run,
                                                        pipeline_settings, rulebook) -> None:
        from lxml import etree

        from termguard import ooxml

        run, _ = completed_run
        self._accept_all(session, run)
        verify.verify_run(session, run.id, settings=pipeline_settings, store=store,
                          rulebook=rulebook, write_outputs=False)

        document = session.exec(select(Document).where(Document.name == "IFU-001.docx")).one()
        data = documents.content(store, documents.latest(session, document.id))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".xml") and name != "[Content_Types].xml":
                    root = etree.fromstring(zf.read(name))
                    assert ooxml.remaining_revisions(root) == []

    def test_rejecting_a_genuine_violation_fails_and_names_it(
        self, session, store, completed_run, pipeline_settings, rulebook
    ) -> None:
        run, _ = completed_run
        target = session.exec(
            select(Change).where(
                Change.mechanism == Mechanism.DETERMINISTIC,
                Change.original_text == "Meridian Pump 2",
            )
        ).first()
        assert target is not None
        review.record_decision(session, target.id, DecisionKind.REJECTED, "qa@meridian",
                               note="deliberately rejected for the test")
        for change in review.pending_changes(session, run.id):
            review.record_decision(session, change.id, DecisionKind.ACCEPTED, "qa@meridian")

        report = verify.verify_run(session, run.id, settings=pipeline_settings,
                                   store=store, rulebook=rulebook, write_outputs=False)
        assert not report.passed
        failing = [f for f in report.files if not f.passed]
        assert any(
            any(h["matched"] == "Meridian Pump 2" for h in f.hits) for f in failing
        )

    def test_an_edited_decision_applies_the_reviewers_wording(
        self, session, store, completed_run, pipeline_settings, rulebook
    ) -> None:
        run, _ = completed_run
        target = session.exec(
            select(Change).where(
                Change.mechanism == Mechanism.DETERMINISTIC,
                Change.original_text == "shall",
            )
        ).first()
        assert target is not None
        review.record_decision(session, target.id, DecisionKind.EDITED, "qa@meridian",
                               final_text="is required to")
        for change in review.pending_changes(session, run.id):
            review.record_decision(session, change.id, DecisionKind.ACCEPTED, "qa@meridian")

        verify.verify_run(session, run.id, settings=pipeline_settings, store=store,
                          rulebook=rulebook, write_outputs=False)
        document = session.get(Document, target.document_id)
        data = documents.content(store, documents.latest(session, document.id))
        from termguard.walker import walk_bytes

        assert "is required to" in " ".join(p.text for p in walk_bytes(data, "x"))

    def test_an_unapproved_edit_fails_as_unexplained(
        self, session, store, completed_run, pipeline_settings, rulebook
    ) -> None:
        """The backstop: prove the tool changed only what it said it changed."""
        run, _ = completed_run
        self._accept_all(session, run)

        document = session.exec(select(Document).where(Document.name == "RMS-001.docx")).one()
        redlined = documents.latest_at_stage(session, document.id, Stage.REDLINED)
        tampered = _hand_edit(documents.content(store, redlined))
        documents.add_version(session, store, document, tampered, Stage.REDLINED,
                              actor="someone with the file open", parent=redlined,
                              run_id=run.id)

        report = verify.verify_run(session, run.id, settings=pipeline_settings,
                                   store=store, rulebook=rulebook, write_outputs=False)
        assert not report.passed
        failed = next(f for f in report.files if f.name == "RMS-001.docx")
        assert failed.unexplained
        assert "unexplained edit" in "; ".join(failed.failure_reasons)

    def test_a_changed_rulebook_refuses_to_verify(self, session, store, completed_run,
                                                  pipeline_settings, rulebook) -> None:
        """A report must never be attributable to a rulebook it was not produced with."""
        run, _ = completed_run
        mutated = rulebook.model_copy(update={"hash": "deadbeefdeadbeef"})
        with pytest.raises(ValueError, match="rulebook has changed"):
            verify.verify_run(session, run.id, settings=pipeline_settings, store=store,
                              rulebook=mutated, write_outputs=False)

    def test_report_renders_markdown_with_the_verdict(self, session, store, completed_run,
                                                      pipeline_settings, rulebook) -> None:
        run, _ = completed_run
        self._accept_all(session, run)
        report = verify.verify_run(session, run.id, settings=pipeline_settings,
                                   store=store, rulebook=rulebook, write_outputs=False)
        markdown = verify.render_markdown(report, rulebook)
        assert "**Result: PASS**" in markdown
        assert rulebook.hash in markdown
        assert "Deterministic (rule engine)" in markdown
        assert "qa@meridian" in markdown


class TestDifferenceExplanation:
    def test_expected_substitution_is_explained(self) -> None:
        assert verify.explain_difference(
            "The physician signs.", "The healthcare provider signs.",
            [("physician", "healthcare provider")],
        )

    def test_extra_edit_is_not_explained(self) -> None:
        assert not verify.explain_difference(
            "The physician signs.", "The healthcare provider signs today.",
            [("physician", "healthcare provider")],
        )

    def test_unexpected_substitution_is_not_explained(self) -> None:
        assert not verify.explain_difference(
            "The physician signs.", "The doctor signs.",
            [("physician", "healthcare provider")],
        )

    def test_several_substitutions_in_one_paragraph(self) -> None:
        assert verify.explain_difference(
            "The physician checks the infusion set and the infusion set again.",
            "The healthcare provider checks the administration set and the administration set again.",
            [("physician", "healthcare provider"),
             ("infusion set", "administration set"),
             ("infusion set", "administration set")],
        )


def _hand_edit(data: bytes) -> bytes:
    """An edit made outside the tool, covered by no decision."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        parts = {n: zf.read(n) for n in zf.namelist()}
    body = parts["word/document.xml"].decode()
    parts["word/document.xml"] = body.replace("</w:t>", " (revised per meeting)</w:t>", 1).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in parts.items():
            out.writestr(name, blob)
    return buffer.getvalue()
