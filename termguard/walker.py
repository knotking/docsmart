"""Layer B (scan), part 1: make every text-bearing paragraph in a .docx addressable.

The most common failure of a manual harmonization pass is missing text that is not in the
body: a product name in a running header, a deprecated term in a table cell on page 7, a
unit in a footnote. Constraint 5 says a hit in a header counts the same as a hit in the
body, and this module is what makes that possible.

It walks the OOXML parts directly rather than going through python-docx's object model,
because python-docx exposes body and tables but not footnotes, endnotes or text boxes, and
``docx-editor`` (verified, see CLAUDE.md) enumerates body and table text only. One uniform
XML walk covers every part and keeps the notion of "location" identical across them.

Yields :class:`WalkedParagraph`, which carries both the reconstructed text and a run map,
so a character span found by the scanner can be written back as a tracked change even when
Word has split the phrase across several runs.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from lxml import etree

from termguard import ooxml
from termguard.ooxml import q

# Which package parts map to which logical document part.
_PART_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^word/document\.xml$"), "body"),
    (re.compile(r"^word/header\d*\.xml$"), "header"),
    (re.compile(r"^word/footer\d*\.xml$"), "footer"),
    (re.compile(r"^word/footnotes\.xml$"), "footnote"),
    (re.compile(r"^word/endnotes\.xml$"), "endnote"),
)

# Reserved note ids: the separator and continuation-separator entries Word requires.
_RESERVED_NOTE_IDS = {"-1", "0"}

PART_ORDER = {"body": 0, "header": 1, "footer": 2, "footnote": 3, "endnote": 4, "textbox": 5}


@dataclass(frozen=True)
class Location:
    """Where a paragraph lives. Stable enough to cite in an audit trail."""

    file: str
    part: str                      # body | header | footer | footnote | endnote | textbox
    part_name: str                 # the package part, e.g. word/header1.xml
    paragraph_index: int           # 0-based, per part
    section: int = 1
    in_table: bool = False
    table: int | None = None
    row: int | None = None
    cell: int | None = None
    is_heading: bool = False
    style: str | None = None
    note_id: str | None = None     # footnote/endnote id, when applicable

    @property
    def container_path(self) -> str:
        """Human-readable container, e.g. ``table 2 / row 3 / cell 1``."""
        if self.in_table and self.table is not None:
            return f"table {self.table} / row {self.row} / cell {self.cell}"
        if self.part in {"header", "footer"}:
            return self.part_name.removeprefix("word/")
        if self.part in {"footnote", "endnote"}:
            return f"{self.part} {self.note_id}"
        if self.part == "textbox":
            return "text box"
        return "body"

    def describe(self) -> str:
        """One-line human description, used in comments and reports."""
        where = self.container_path
        if self.part == "body" and not self.in_table:
            where = "heading" if self.is_heading else "body"
        return f"{self.file} / {where} / paragraph {self.paragraph_index}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "part": self.part,
            "part_name": self.part_name,
            "paragraph_index": self.paragraph_index,
            "section": self.section,
            "in_table": self.in_table,
            "table": self.table,
            "row": self.row,
            "cell": self.cell,
            "is_heading": self.is_heading,
            "style": self.style,
            "note_id": self.note_id,
            "container_path": self.container_path,
        }


@dataclass
class WalkedParagraph:
    """One paragraph, its text, and the machinery to edit it."""

    location: Location
    text: str
    element: etree._Element = field(repr=False)
    run_map: list[tuple[etree._Element, int, int]] = field(default_factory=list, repr=False)
    root: etree._Element | None = field(default=None, repr=False)

    def nodes_for_span(self, start: int, end: int) -> list[tuple[etree._Element, int, int]]:
        """The ``w:t`` nodes covering a character span, with node-local offsets."""
        return ooxml.nodes_for_span(self.run_map, start, end)


def _classify_part(name: str) -> str | None:
    for pattern, part in _PART_PATTERNS:
        if pattern.match(name):
            return part
    return None


def _note_id(paragraph: etree._Element, note_tag: str) -> str | None:
    note = ooxml.ancestor(paragraph, q(note_tag))
    return note.get(q("id")) if note is not None else None


def _is_reserved_note(paragraph: etree._Element, note_tag: str) -> bool:
    """Separator / continuation-separator notes carry no authored text."""
    note = ooxml.ancestor(paragraph, q(note_tag))
    if note is None:
        return False
    if note.get(q("type")) in {"separator", "continuationSeparator"}:
        return True
    return note.get(q("id")) in _RESERVED_NOTE_IDS


def walk_part(
    file_name: str, part_name: str, part: str, root: etree._Element
) -> Iterator[WalkedParagraph]:
    """Walk one package part, yielding its paragraphs in document order.

    Text-box paragraphs inside ``word/document.xml`` are reported as part ``textbox``
    rather than ``body``, because a reviewer looking for them needs to know they are in a
    floating shape, not the text flow.
    """
    note_tag = {"footnote": "footnote", "endnote": "endnote"}.get(part)
    counters: dict[str, int] = {}

    for paragraph in ooxml.iter_paragraphs(root):
        if note_tag and _is_reserved_note(paragraph, note_tag):
            continue

        in_textbox = ooxml.has_ancestor(paragraph, q("txbxContent"))
        effective_part = "textbox" if (part == "body" and in_textbox) else part

        text = ooxml.paragraph_text(paragraph)
        if not text.strip():
            # Empty paragraphs still occupy an index, so count them before skipping.
            counters[effective_part] = counters.get(effective_part, 0) + 1
            continue

        index = counters.get(effective_part, 0)
        counters[effective_part] = index + 1

        style = ooxml.paragraph_style(paragraph)
        coords = ooxml.table_coordinates(paragraph, root) if not in_textbox else None

        location = Location(
            file=file_name,
            part=effective_part,
            part_name=part_name,
            paragraph_index=index,
            section=ooxml.section_index(paragraph, root) if part == "body" else 1,
            in_table=coords is not None,
            table=coords[0] if coords else None,
            row=coords[1] if coords else None,
            cell=coords[2] if coords else None,
            is_heading=ooxml.is_heading_style(style),
            style=style,
            note_id=_note_id(paragraph, note_tag) if note_tag else None,
        )
        yield WalkedParagraph(
            location=location,
            text=text,
            element=paragraph,
            run_map=ooxml.build_run_map(paragraph),
            root=root,
        )


def walk(path: Path | str, *, file_name: str | None = None) -> Iterator[WalkedParagraph]:
    """Walk every text-bearing paragraph of a .docx, across every part.

    Parts are visited in a stable order (body, headers, footers, footnotes, endnotes) so
    two walks of the same file always agree, which matters when a scan report is compared
    against a re-scan during verification.
    """
    path = Path(path)
    name = file_name or path.name

    with zipfile.ZipFile(path) as zf:
        targets: list[tuple[str, str]] = []
        for entry in zf.namelist():
            part = _classify_part(entry)
            if part is not None:
                targets.append((entry, part))
        targets.sort(key=lambda t: (PART_ORDER.get(t[1], 99), t[0]))

        for part_name, part in targets:
            try:
                root = etree.fromstring(zf.read(part_name))
            except etree.XMLSyntaxError:  # pragma: no cover - malformed package
                continue
            yield from walk_part(name, part_name, part, root)


def walk_bytes(data: bytes, file_name: str) -> Iterator[WalkedParagraph]:
    """Walk a document held in memory. Used when reading from the object store."""
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        targets = sorted(
            ((n, p) for n in zf.namelist() if (p := _classify_part(n)) is not None),
            key=lambda t: (PART_ORDER.get(t[1], 99), t[0]),
        )
        for part_name, part in targets:
            try:
                root = etree.fromstring(zf.read(part_name))
            except etree.XMLSyntaxError:  # pragma: no cover
                continue
            yield from walk_part(file_name, part_name, part, root)


def part_counts(path: Path | str) -> dict[str, int]:
    """How many paragraphs each part contributes. Useful as a coverage sanity check."""
    counts: dict[str, int] = {}
    for para in walk(path):
        counts[para.location.part] = counts.get(para.location.part, 0) + 1
    return counts
