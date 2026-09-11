"""Layer B part 1: the walker must reach every planted violation, in every part."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from termguard.walker import Location, walk, walk_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def ground_truth(corpus_dir: Path) -> dict:
    return json.loads((corpus_dir / "ground_truth.json").read_text())


@pytest.fixture(scope="module")
def walked(corpus_dir: Path) -> dict[tuple[str, str, int], str]:
    """Every paragraph in the corpus, keyed by (file, part, paragraph_index)."""
    index: dict[tuple[str, str, int], str] = {}
    for path in sorted(corpus_dir.glob("*.docx")):
        for para in walk(path):
            loc = para.location
            index[(loc.file, loc.part, loc.paragraph_index)] = para.text
    return index


class TestReachability:
    def test_every_planted_violation_is_reachable(self, ground_truth, walked) -> None:
        """The acceptance criterion for this layer: no planted item may be unreachable."""
        misses: list[str] = []
        for item in ground_truth["items"]:
            key = (item["file"], item["part"], item["paragraph_index"])
            text = walked.get(key)
            if text is None:
                misses.append(f"{key} - no such paragraph")
            elif item["text"] not in text:
                misses.append(f"{key} - {item['text']!r} not in {text[:70]!r}")

        assert not misses, (
            f"{len(misses)} of {len(ground_truth['items'])} planted items unreachable:\n  "
            + "\n  ".join(misses[:20])
        )

    @pytest.mark.parametrize("part", ["header", "footer", "footnote"])
    def test_non_body_parts_are_covered(self, ground_truth, walked, part: str) -> None:
        """Headers, footers and footnotes are exactly where manual review fails."""
        items = [i for i in ground_truth["items"] if i["part"] == part]
        assert items, f"no ground truth planted in {part}"
        for item in items:
            text = walked[(item["file"], part, item["paragraph_index"])]
            assert item["text"] in text

    def test_table_cell_violations_are_reachable(self, ground_truth, walked) -> None:
        items = [i for i in ground_truth["items"] if i["in_table"]]
        assert len(items) >= 6
        for item in items:
            text = walked[(item["file"], item["part"], item["paragraph_index"])]
            assert item["text"] in text

    def test_heading_violations_are_reachable_and_flagged(self, corpus_dir, ground_truth) -> None:
        headings: dict[tuple[str, int], bool] = {}
        for path in sorted(corpus_dir.glob("*.docx")):
            for para in walk(path):
                if para.location.part == "body":
                    headings[(para.location.file, para.location.paragraph_index)] = (
                        para.location.is_heading
                    )
        items = [i for i in ground_truth["items"] if i["is_heading"]]
        assert len(items) >= 4
        for item in items:
            assert headings[(item["file"], item["paragraph_index"])], (
                f"{item['file']} para {item['paragraph_index']} not detected as a heading"
            )


class TestStructure:
    def test_all_parts_present_somewhere_in_the_corpus(self, corpus_dir: Path) -> None:
        parts: set[str] = set()
        for path in corpus_dir.glob("*.docx"):
            for para in walk(path):
                parts.add(para.location.part)
        assert {"body", "header", "footer", "footnote"} <= parts

    def test_table_coordinates_are_one_based_and_ordered(self, corpus_dir: Path) -> None:
        cells = [p.location for p in walk(corpus_dir / "RMS-001.docx") if p.location.in_table]
        assert cells, "expected table cells"
        assert all(c.table >= 1 and c.row >= 1 and c.cell >= 1 for c in cells)
        assert cells[0].container_path == "table 1 / row 1 / cell 1"

    def test_run_map_covers_the_text_contiguously(self, corpus_dir: Path) -> None:
        for para in walk(corpus_dir / "IFU-001.docx"):
            rebuilt = "".join(node.text or "" for node, _, _ in para.run_map)
            assert rebuilt == para.text
            offsets = [(s, e) for _, s, e in para.run_map]
            for (_, prev_end), (next_start, _) in zip(offsets, offsets[1:]):
                assert prev_end == next_start

    def test_span_lookup_survives_runs_split_by_word(self, corpus_dir: Path) -> None:
        """Word splits phrases across runs freely; a span must still resolve."""
        found = False
        for para in walk(corpus_dir / "IFU-001.docx"):
            start = para.text.find("Meridian Pump 2")
            if start < 0:
                continue
            found = True
            nodes = para.nodes_for_span(start, start + len("Meridian Pump 2"))
            assert nodes
            recovered = "".join(
                (node.text or "")[local_start:local_end] for node, local_start, local_end in nodes
            )
            assert recovered == "Meridian Pump 2"
        assert found, "expected the legacy product name in IFU-001"

    def test_walk_bytes_matches_walk(self, corpus_dir: Path) -> None:
        """Reading from the object store must give identical results to reading from disk."""
        path = corpus_dir / "SOP-001.docx"
        from_disk = [(p.location.part, p.location.paragraph_index, p.text) for p in walk(path)]
        from_bytes = [
            (p.location.part, p.location.paragraph_index, p.text)
            for p in walk_bytes(path.read_bytes(), path.name)
        ]
        assert from_disk == from_bytes

    def test_walking_is_deterministic(self, corpus_dir: Path) -> None:
        """A re-scan during verification compares against the original walk order."""
        path = corpus_dir / "CER-001.docx"
        first = [(p.location.part, p.location.paragraph_index) for p in walk(path)]
        second = [(p.location.part, p.location.paragraph_index) for p in walk(path)]
        assert first == second

    def test_controls_contain_no_deprecated_terms(self, corpus_dir: Path) -> None:
        text = " ".join(p.text for p in walk(corpus_dir / "CTL-001.docx")).lower()
        for term in ("meridian pump 2", "side effect", "infusion set", "labelling", "shall"):
            assert term not in text


class TestLocation:
    def test_describe_is_readable(self) -> None:
        loc = Location(file="IFU-001.docx", part="body", part_name="word/document.xml",
                       paragraph_index=4, in_table=True, table=2, row=3, cell=1)
        assert loc.describe() == "IFU-001.docx / table 2 / row 3 / cell 1 / paragraph 4"

    def test_header_container_path_names_the_part(self) -> None:
        loc = Location(file="x.docx", part="header", part_name="word/header1.xml",
                       paragraph_index=0)
        assert loc.container_path == "header1.xml"

    def test_footnote_container_path_names_the_note(self) -> None:
        loc = Location(file="x.docx", part="footnote", part_name="word/footnotes.xml",
                       paragraph_index=0, note_id="1")
        assert loc.container_path == "footnote 1"
