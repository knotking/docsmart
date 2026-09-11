"""Layer S (substrate): shared OOXML primitives.

Namespace constants and element helpers used by :mod:`termguard.walker` (reading every
part of a document) and the raw-XML half of :mod:`termguard.redline` (writing tracked
changes into the parts ``docx-editor`` does not reach).

Everything here operates on ``lxml`` elements from a document's parts. Nothing here opens
or saves a file; that is the caller's job.
"""

from __future__ import annotations

from typing import Iterator

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XML = "http://www.w3.org/XML/1998/namespace"

NSMAP = {"w": W, "r": R, "mc": MC}

# Heading styles: Word's built-in ids plus the Title style.
HEADING_STYLE_PREFIXES = ("Heading", "heading")
TITLE_STYLES = {"Title", "Subtitle"}


def q(tag: str) -> str:
    """Qualify a ``w:``-namespaced tag name."""
    return f"{{{W}}}{tag}"


def qmc(tag: str) -> str:
    return f"{{{MC}}}{tag}"


def has_ancestor(element: etree._Element, tag: str, stop_at: etree._Element | None = None) -> bool:
    """True if ``element`` has an ancestor with the qualified ``tag``."""
    node = element.getparent()
    while node is not None and node is not stop_at:
        if node.tag == tag:
            return True
        node = node.getparent()
    return False


def ancestor(element: etree._Element, tag: str) -> etree._Element | None:
    """Nearest ancestor with the qualified ``tag``, or None."""
    node = element.getparent()
    while node is not None:
        if node.tag == tag:
            return node
        node = node.getparent()
    return None


def paragraph_style(paragraph: etree._Element) -> str | None:
    """The paragraph's explicit style id, or None."""
    ppr = paragraph.find(q("pPr"))
    if ppr is None:
        return None
    style = ppr.find(q("pStyle"))
    if style is None:
        return None
    return style.get(q("val"))


def is_heading_style(style: str | None) -> bool:
    if not style:
        return False
    return style.startswith(HEADING_STYLE_PREFIXES) or style in TITLE_STYLES


def iter_paragraphs(root: etree._Element) -> Iterator[etree._Element]:
    """Every ``w:p`` in document order, excluding markup-compatibility fallbacks.

    A drawing's text box is serialized twice - once under ``mc:Choice`` and once under
    ``mc:Fallback`` - so yielding both would double-count every text-box paragraph and
    inflate the hit counts. Only the Choice branch is yielded.
    """
    for paragraph in root.iter(q("p")):
        if has_ancestor(paragraph, qmc("Fallback")):
            continue
        yield paragraph


def visible_text_nodes(paragraph: etree._Element) -> list[etree._Element]:
    """The ``w:t`` nodes that contribute to a paragraph's visible text, in order.

    Excludes text inside ``w:del`` (already struck by a tracked deletion) and field
    instruction codes (``w:instrText`` is a distinct element, so it is skipped naturally).
    A field's *cached result* is an ordinary ``w:t`` and is therefore included, which is
    what we want: the reader sees it, so the scanner must too.
    """
    nodes: list[etree._Element] = []
    for node in paragraph.iter(q("t")):
        if has_ancestor(node, q("del"), stop_at=paragraph):
            continue
        nodes.append(node)
    return nodes


def paragraph_text(paragraph: etree._Element) -> str:
    """Visible text of a paragraph, reconstructed across runs."""
    return "".join(node.text or "" for node in visible_text_nodes(paragraph))


def build_run_map(paragraph: etree._Element) -> list[tuple[etree._Element, int, int]]:
    """Map character offsets to the ``w:t`` nodes that carry them.

    Returns ``(node, start, end)`` triples with ``end`` exclusive, covering the paragraph
    text contiguously. This is what lets a character span found by the scanner be written
    back as a tracked change even when Word has split the phrase across several runs.
    """
    run_map: list[tuple[etree._Element, int, int]] = []
    offset = 0
    for node in visible_text_nodes(paragraph):
        text = node.text or ""
        run_map.append((node, offset, offset + len(text)))
        offset += len(text)
    return run_map


def nodes_for_span(
    run_map: list[tuple[etree._Element, int, int]], start: int, end: int
) -> list[tuple[etree._Element, int, int]]:
    """The ``w:t`` nodes overlapping ``[start, end)``, with node-local offsets.

    Returns ``(node, local_start, local_end)``. A span confined to one run yields one
    triple; a span split across runs yields several, in document order.
    """
    out: list[tuple[etree._Element, int, int]] = []
    for node, node_start, node_end in run_map:
        if node_end <= start or node_start >= end:
            continue
        out.append((node, max(start, node_start) - node_start, min(end, node_end) - node_start))
    return out


def table_coordinates(paragraph: etree._Element, root: etree._Element) -> tuple[int, int, int] | None:
    """1-based ``(table, row, cell)`` for a paragraph inside a table cell, else None.

    The table index counts tables in document order at any nesting depth, matching how a
    reader would say "table 2" when pointing at a page.
    """
    cell = ancestor(paragraph, q("tc"))
    if cell is None:
        return None
    row = ancestor(cell, q("tr"))
    table = ancestor(cell, q("tbl"))
    if row is None or table is None:
        return None

    tables = [t for t in root.iter(q("tbl")) if not has_ancestor(t, qmc("Fallback"))]
    try:
        table_index = tables.index(table) + 1
    except ValueError:  # pragma: no cover - table not reachable from this root
        table_index = 1

    rows = [r for r in table.iterchildren(q("tr"))]
    row_index = rows.index(row) + 1 if row in rows else 1
    cells = [c for c in row.iterchildren(q("tc"))]
    cell_index = cells.index(cell) + 1 if cell in cells else 1
    return table_index, row_index, cell_index


def section_index(paragraph: etree._Element, root: etree._Element) -> int:
    """1-based section number.

    A paragraph carrying a direct ``w:sectPr`` closes its section, so it belongs to the
    section it closes and the next paragraph opens the following one.
    """
    index = 1
    for candidate in iter_paragraphs(root):
        if candidate is paragraph:
            return index
        ppr = candidate.find(q("pPr"))
        if ppr is not None and ppr.find(q("sectPr")) is not None:
            index += 1
    return index
