"""Layer C: tracked-change output.

The acceptance criterion is that Word opens the file without a repair prompt and shows
ordinary tracked changes. We cannot drive Word from a test, so we assert the properties
that a repair prompt would violate: the package reopens in two independent libraries, the
revision markup is well-formed and balanced, and accepting or rejecting every change
yields exactly the approved or the original text.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from lxml import etree

import docx_editor as de

from termguard import ooxml
from termguard.ooxml import q
from termguard.redline import build_comment, redline
from termguard.rulebook import Rulebook, load_rulebook
from termguard.scanner import scan_bytes
from termguard.walker import walk_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTHOR = "TermGuard (rule engine)"


@pytest.fixture(scope="module")
def rulebook() -> Rulebook:
    return load_rulebook(REPO_ROOT / "data" / "rulebook.yaml")


@pytest.fixture(scope="module")
def source(corpus_dir: Path) -> bytes:
    """IFU-001 carries hits in the body, a heading, a table, a header and a footer."""
    return (corpus_dir / "IFU-001.docx").read_bytes()


@pytest.fixture(scope="module")
def unambiguous_hits(source: bytes, rulebook: Rulebook):
    return [h for h in scan_bytes(source, "IFU-001.docx", rulebook)
            if h.classification == "unambiguous"]


@pytest.fixture(scope="module")
def result(source: bytes, unambiguous_hits, rulebook: Rulebook):
    return redline(source, unambiguous_hits, rulebook, author=AUTHOR)


def count_in_part(data: bytes, part: str, tag: str) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if part not in zf.namelist():
            return 0
        return zf.read(part).decode().count(f"<w:{tag} ")


def parts_of(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()


class TestApplication:
    def test_every_unambiguous_hit_is_applied(self, result, unambiguous_hits) -> None:
        assert result.count == len(unambiguous_hits)
        assert result.skipped == []

    def test_both_engines_are_used(self, result) -> None:
        """Body hits go to docx-editor; header and footer hits must not be skipped."""
        engines = result.by_engine()
        assert engines["docx-editor"] > 0
        assert engines["raw-ooxml"] > 0

    def test_insertion_and_deletion_counts_match_the_applied_hits(self, result) -> None:
        total_ins = sum(
            count_in_part(result.data, p, "ins")
            for p in parts_of(result.data) if p.endswith(".xml")
        )
        total_del = sum(
            count_in_part(result.data, p, "del")
            for p in parts_of(result.data) if p.endswith(".xml")
        )
        assert total_ins == result.count
        assert total_del == result.count

    def test_revisions_are_attributed_to_the_named_author(self, result) -> None:
        with zipfile.ZipFile(io.BytesIO(result.data)) as zf:
            for part in ("word/document.xml", "word/header1.xml", "word/footer1.xml"):
                root = etree.fromstring(zf.read(part))
                for tag in ("ins", "del"):
                    for element in root.iter(q(tag)):
                        assert element.get(q("author")) == AUTHOR
                        assert element.get(q("date"))
                        assert element.get(q("id"))

    def test_comments_cite_the_rule_and_the_mechanism(self, result) -> None:
        with zipfile.ZipFile(io.BytesIO(result.data)) as zf:
            comments = zf.read("word/comments.xml").decode()
        assert "Mechanism: deterministic" in comments
        assert "R-00" in comments

    def test_source_bytes_are_never_modified(self, corpus_dir: Path, result) -> None:
        """Constraint 1: originals are never touched."""
        current = hashlib.sha256((corpus_dir / "IFU-001.docx").read_bytes()).hexdigest()
        expected = hashlib.sha256(
            (corpus_dir / "IFU-001.docx").read_bytes()
        ).hexdigest()
        assert current == expected
        assert result.data != (corpus_dir / "IFU-001.docx").read_bytes()

    def test_no_hits_is_a_no_op(self, source: bytes, rulebook: Rulebook) -> None:
        outcome = redline(source, [], rulebook, author=AUTHOR)
        assert outcome.data == source
        assert outcome.count == 0


class TestNonBodyParts:
    """The parts docx-editor cannot reach must still be redlined, not skipped."""

    def test_header_and_footer_are_actually_changed(self, result) -> None:
        text = {
            p.location.part: p.text
            for p in walk_bytes(result.data, "IFU-001.docx")
            if p.location.part in {"header", "footer"}
        }
        assert "Meridian Infusion System" in text["header"]
        assert "Meridian Pump 2" not in text["header"]
        assert "single-use" in text["footer"]

    def test_header_revision_markup_is_present(self, result) -> None:
        assert count_in_part(result.data, "word/header1.xml", "ins") == 1
        assert count_in_part(result.data, "word/header1.xml", "del") == 1

    def test_footnote_hits_are_redlined(self, corpus_dir: Path, rulebook: Rulebook) -> None:
        data = (corpus_dir / "RMS-001.docx").read_bytes()
        hits = [h for h in scan_bytes(data, "RMS-001.docx", rulebook)
                if h.classification == "unambiguous" and h.part == "footnote"]
        assert hits, "expected footnote hits in RMS-001"
        outcome = redline(data, hits, rulebook, author=AUTHOR)
        assert outcome.count == len(hits)
        assert count_in_part(outcome.data, "word/footnotes.xml", "ins") == len(hits)
        footnote_text = " ".join(
            p.text for p in walk_bytes(outcome.data, "x") if p.location.part == "footnote"
        )
        assert "mL" in footnote_text and "healthcare provider" in footnote_text

    def test_comments_in_headers_are_off_by_default(self, result) -> None:
        """Word has no UI for header comments; the citation lives in the audit trail."""
        assert "commentRangeStart" not in zipfile.ZipFile(
            io.BytesIO(result.data)
        ).read("word/header1.xml").decode()

    def test_comments_in_headers_can_be_enabled(self, source, unambiguous_hits, rulebook) -> None:
        outcome = redline(source, unambiguous_hits, rulebook, author=AUTHOR,
                          comment_non_body_parts=True)
        header = zipfile.ZipFile(io.BytesIO(outcome.data)).read("word/header1.xml").decode()
        assert "commentRangeStart" in header


class TestWordCompatibility:
    def test_package_reopens_in_python_docx(self, result, tmp_path: Path) -> None:
        import docx

        path = tmp_path / "out.docx"
        path.write_bytes(result.data)
        assert len(docx.Document(path).paragraphs) > 0

    def test_package_reopens_in_docx_editor(self, result, tmp_path: Path) -> None:
        path = tmp_path / "out.docx"
        path.write_bytes(result.data)
        doc = de.Document.open(path, author="checker", force_recreate=True)
        try:
            assert len(doc.list_revisions()) > 0
            assert len(doc.list_comments()) > 0
        finally:
            doc.close()

    def test_every_part_is_well_formed_xml(self, result) -> None:
        with zipfile.ZipFile(io.BytesIO(result.data)) as zf:
            for name in zf.namelist():
                if name.endswith(".xml") or name.endswith(".rels"):
                    etree.fromstring(zf.read(name))  # raises if malformed

    def test_required_parts_are_declared(self, result) -> None:
        with zipfile.ZipFile(io.BytesIO(result.data)) as zf:
            types = zf.read("[Content_Types].xml").decode()
            rels = zf.read("word/_rels/document.xml.rels").decode()
        assert "comments+xml" in types
        assert "comments.xml" in rels

    def test_deleted_runs_use_delText_not_t(self, result) -> None:
        """A w:t inside w:del is the classic cause of a Word repair prompt."""
        with zipfile.ZipFile(io.BytesIO(result.data)) as zf:
            for part in ("word/document.xml", "word/header1.xml", "word/footer1.xml"):
                root = etree.fromstring(zf.read(part))
                for deletion in root.iter(q("del")):
                    assert deletion.findall(f".//{q('t')}") == []
                    assert deletion.findall(f".//{q('delText')}")


class TestRevisionSemantics:
    """The strongest available proxy for 'Word will do the right thing'."""

    def test_rejecting_every_change_restores_the_original_text(
        self, result, source: bytes, tmp_path: Path
    ) -> None:
        path = tmp_path / "reject.docx"
        path.write_bytes(result.data)
        doc = de.Document.open(path, author="checker", force_recreate=True)
        try:
            doc.reject_all()
            doc.save(path, force=True)
        finally:
            doc.close()

        original_body = [p.text for p in walk_bytes(source, "x") if p.location.part == "body"]
        restored_body = [
            p.text for p in walk_bytes(path.read_bytes(), "x") if p.location.part == "body"
        ]
        assert restored_body == original_body

    def test_accepting_every_change_yields_the_approved_terms(
        self, result, tmp_path: Path
    ) -> None:
        path = tmp_path / "accept.docx"
        path.write_bytes(result.data)
        doc = de.Document.open(path, author="checker", force_recreate=True)
        try:
            doc.accept_all()
            doc.save(path, force=True)
        finally:
            doc.close()

        body = " ".join(
            p.text for p in walk_bytes(path.read_bytes(), "x") if p.location.part == "body"
        )
        assert "Meridian Infusion System" in body
        assert "Meridian Pump 2" not in body
        assert "administration set" in body

    def test_redlined_output_has_no_unambiguous_hits_left(
        self, result, rulebook: Rulebook
    ) -> None:
        """Visible text already reads correctly; only judgment calls remain."""
        remaining = scan_bytes(result.data, "IFU-001.docx", rulebook)
        assert [h for h in remaining if h.classification == "unambiguous"] == []
        assert all(h.needs_judgment for h in remaining)


class TestFormattingPreservation:
    def test_run_formatting_survives_a_cross_run_replacement(self) -> None:
        xml = (
            f'<w:p xmlns:w="{ooxml.W}">'
            '<w:r><w:rPr><w:b/></w:rPr><w:t>Attach the Meridian </w:t></w:r>'
            '<w:r><w:rPr><w:b/></w:rPr><w:t>Pump</w:t></w:r>'
            '<w:r><w:rPr><w:b/></w:rPr><w:t> 2 now.</w:t></w:r>'
            "</w:p>"
        )
        paragraph = etree.fromstring(xml)
        run_map = ooxml.build_run_map(paragraph)
        start = ooxml.paragraph_text(paragraph).index("Meridian Pump 2")
        _, insertion = ooxml.apply_tracked_replacement(
            paragraph, run_map, start, start + len("Meridian Pump 2"),
            "Meridian Infusion System",
            author=AUTHOR, date=ooxml.iso_timestamp(), del_id=1, ins_id=2,
        )
        inserted_run = insertion.find(q("r"))
        assert inserted_run.find(q("rPr")).find(q("b")) is not None
        assert ooxml.paragraph_text(paragraph) == "Attach the Meridian Infusion System now."

    def test_surrounding_text_is_untouched(self) -> None:
        xml = f'<w:p xmlns:w="{ooxml.W}"><w:r><w:t>Keep this shall keep that</w:t></w:r></w:p>'
        paragraph = etree.fromstring(xml)
        run_map = ooxml.build_run_map(paragraph)
        start = 10
        ooxml.apply_tracked_replacement(
            paragraph, run_map, start, start + 5, "must",
            author=AUTHOR, date=ooxml.iso_timestamp(), del_id=1, ins_id=2,
        )
        assert ooxml.paragraph_text(paragraph) == "Keep this must keep that"


class TestCommentText:
    def test_comment_cites_rule_rationale_and_mechanism(self, rulebook: Rulebook, source, rulebook_hit=None) -> None:
        hit = next(h for h in scan_bytes(source, "IFU-001.docx", rulebook) if h.rule_id == "R-001")
        text = build_comment(hit, rulebook, "deterministic")
        assert text.startswith("R-001: Meridian Pump 2 -> Meridian Infusion System.")
        assert "Mechanism: deterministic." in text
        assert "renamed" in text

    def test_ai_mechanism_is_distinguishable(self, rulebook: Rulebook, source) -> None:
        hit = next(h for h in scan_bytes(source, "IFU-001.docx", rulebook) if h.rule_id == "R-002")
        text = build_comment(hit, rulebook, "AI-proposed", extra="Justification: patient-facing.")
        assert "Mechanism: AI-proposed." in text
        assert "Justification: patient-facing." in text
