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


# --------------------------------------------------------------- tracked changes
#
# Everything below writes revision markup into parts that ``docx-editor`` does not reach
# (headers, footers, footnotes, endnotes, text boxes). The output has to be markup Word
# accepts without a repair prompt, so it follows the same shape Word itself produces:
# a ``w:del`` whose runs carry ``w:delText``, and a sibling ``w:ins`` carrying the
# replacement, both stamped with an author and an ISO-8601 date.

from copy import deepcopy  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

DELETABLE_ATTRS = (q("id"), q("author"), q("date"))


def iso_timestamp(moment: datetime | None = None) -> str:
    """Revision timestamp in the form Word writes: UTC, second precision, trailing Z."""
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clone_run_shell(run: etree._Element) -> etree._Element:
    """A copy of a run carrying its formatting (``w:rPr``) but no content.

    Preserving ``w:rPr`` is what keeps bold, italics and character styles intact across a
    replacement - constraint 1 says we may not quietly restyle a document either.
    """
    new_run = etree.Element(q("r"))
    rpr = run.find(q("rPr"))
    if rpr is not None:
        new_run.append(deepcopy(rpr))
    return new_run


def _text_run(template: etree._Element, text: str, *, deleted: bool = False) -> etree._Element:
    """A run carrying ``text``, formatted like ``template``."""
    run = _clone_run_shell(template)
    node = etree.SubElement(run, q("delText") if deleted else q("t"))
    node.text = text
    node.set(f"{{{XML}}}space", "preserve")
    return run


def _revision_wrapper(tag: str, rev_id: int, author: str, date: str) -> etree._Element:
    element = etree.Element(q(tag))
    element.set(q("id"), str(rev_id))
    element.set(q("author"), author)
    element.set(q("date"), date)
    return element


def apply_tracked_replacement(
    paragraph: etree._Element,
    run_map: list[tuple[etree._Element, int, int]],
    start: int,
    end: int,
    replacement: str,
    *,
    author: str,
    date: str,
    del_id: int,
    ins_id: int,
) -> tuple[etree._Element, etree._Element]:
    """Replace ``[start, end)`` with ``replacement`` as a tracked change.

    Handles a span split across several runs: each affected run is cut into the part
    before the span, the deleted part, and the part after. The replacement is inserted
    once, at the position of the first affected run, so the reader sees one clean
    "was X, now Y" pair rather than one per run boundary.

    Returns the ``(w:del, w:ins)`` elements written.
    """
    affected = nodes_for_span(run_map, start, end)
    if not affected:
        raise ValueError(f"span [{start}, {end}) does not resolve to any run")

    del_element = _revision_wrapper("del", del_id, author, date)
    ins_element = _revision_wrapper("ins", ins_id, author, date)

    first_run: etree._Element | None = None
    anchor_parent: etree._Element | None = None
    anchor_position: int | None = None

    for node, local_start, local_end in affected:
        run = node.getparent()
        if run is None or run.tag != q("r"):
            raise ValueError("text node is not inside a run")
        parent = run.getparent()
        if parent is None:
            raise ValueError("run is not attached to a paragraph")

        text = node.text or ""
        before, middle, after = text[:local_start], text[local_start:local_end], text[local_end:]
        position = list(parent).index(run)

        if first_run is None:
            first_run = run
            anchor_parent = parent
            anchor_position = position

        replacements: list[etree._Element] = []
        if before:
            replacements.append(_text_run(run, before))
        del_element.append(_text_run(run, middle, deleted=True))
        if after:
            replacements.append(_text_run(run, after))

        parent.remove(run)
        for offset, new_run in enumerate(replacements):
            parent.insert(position + offset, new_run)
        # Record where the revision pair goes: immediately after the "before" fragment.
        if run is first_run:
            anchor_position = position + (1 if before else 0)

    assert anchor_parent is not None and anchor_position is not None
    ins_element.append(_text_run(first_run, replacement))  # type: ignore[arg-type]
    anchor_parent.insert(anchor_position, ins_element)
    anchor_parent.insert(anchor_position, del_element)
    return del_element, ins_element


def add_comment_range(
    paragraph: etree._Element,
    anchor: etree._Element,
    comment_id: int,
) -> None:
    """Wrap ``anchor`` in a comment range and append the reference run.

    ``anchor`` is normally the ``w:ins`` element written by
    :func:`apply_tracked_replacement`, so the comment points at the change itself.
    """
    parent = anchor.getparent()
    if parent is None:  # pragma: no cover - defensive
        return
    position = list(parent).index(anchor)

    start = etree.Element(q("commentRangeStart"))
    start.set(q("id"), str(comment_id))
    end = etree.Element(q("commentRangeEnd"))
    end.set(q("id"), str(comment_id))

    reference_run = etree.Element(q("r"))
    rpr = etree.SubElement(reference_run, q("rPr"))
    style = etree.SubElement(rpr, q("rStyle"))
    style.set(q("val"), "CommentReference")
    reference = etree.SubElement(reference_run, q("commentReference"))
    reference.set(q("id"), str(comment_id))

    parent.insert(position, start)
    parent.insert(position + 2, end)          # after the anchor
    parent.insert(position + 3, reference_run)


def build_comment_element(
    comment_id: int, author: str, initials: str, date: str, text: str
) -> etree._Element:
    """A ``w:comment`` element for comments.xml."""
    comment = etree.Element(q("comment"))
    comment.set(q("id"), str(comment_id))
    comment.set(q("author"), author)
    comment.set(q("initials"), initials)
    comment.set(q("date"), date)

    paragraph = etree.SubElement(comment, q("p"))
    ppr = etree.SubElement(paragraph, q("pPr"))
    pstyle = etree.SubElement(ppr, q("pStyle"))
    pstyle.set(q("val"), "CommentText")

    annotation_run = etree.SubElement(paragraph, q("r"))
    rpr = etree.SubElement(annotation_run, q("rPr"))
    rstyle = etree.SubElement(rpr, q("rStyle"))
    rstyle.set(q("val"), "CommentReference")
    etree.SubElement(annotation_run, q("annotationRef"))

    text_run = etree.SubElement(paragraph, q("r"))
    node = etree.SubElement(text_run, q("t"))
    node.text = text
    node.set(f"{{{XML}}}space", "preserve")
    return comment


# ----------------------------------------------------------- resolving revisions
#
# Turning a redlined document into an "as-accepted" one. Word does this when a reviewer
# clicks Accept; :mod:`termguard.verify` does it here so the final text can be re-scanned
# and proved clean. Works uniformly across every part, because ``docx-editor``'s revision
# ids are the ``w:id`` attributes it writes, and the raw engine allocates ids above
# ``RAW_ID_BASE`` - so one id space addresses every change in the package.


def find_revisions(root: etree._Element, revision_id: int) -> list[etree._Element]:
    """Every ``w:ins``/``w:del`` element carrying this ``w:id``."""
    target = str(revision_id)
    return [
        element
        for element in root.iter(q("ins"), q("del"))
        if element.get(q("id")) == target
    ]


def _unwrap(element: etree._Element) -> None:
    """Replace an element with its children, keeping document order."""
    parent = element.getparent()
    if parent is None:
        return
    position = list(parent).index(element)
    for offset, child in enumerate(list(element)):
        parent.insert(position + offset, child)
    parent.remove(element)


def _deleted_to_visible(run_container: etree._Element) -> None:
    """Convert ``w:delText`` back to ``w:t`` so rejected text becomes visible again."""
    for node in run_container.iter(q("delText")):
        node.tag = q("t")


def _set_run_text(container: etree._Element, text: str) -> None:
    """Force a revision's runs to carry exactly ``text`` (used for reviewer edits)."""
    nodes = list(container.iter(q("t")))
    if not nodes:
        return
    nodes[0].text = text
    nodes[0].set(f"{{{XML}}}space", "preserve")
    for extra in nodes[1:]:
        extra.text = ""


def resolve_revision(
    root: etree._Element,
    revision_id: int,
    *,
    accept: bool,
    replacement_text: str | None = None,
) -> int:
    """Accept or reject one revision, in place. Returns how many elements were resolved.

    Accepting keeps the insertion's text and discards the deletion; rejecting does the
    opposite. ``replacement_text`` overrides the inserted text, which is how a reviewer's
    edited wording is applied instead of the proposed wording.
    """
    resolved = 0
    for element in find_revisions(root, revision_id):
        is_insertion = element.tag == q("ins")
        if accept:
            if is_insertion:
                if replacement_text is not None:
                    _set_run_text(element, replacement_text)
                _unwrap(element)
            else:
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
        else:
            if is_insertion:
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
            else:
                _deleted_to_visible(element)
                _unwrap(element)
        resolved += 1
    return resolved


def strip_comment_markers(root: etree._Element) -> int:
    """Remove comment ranges and reference runs from a part.

    The final document is the clean, as-approved text; review annotations do not belong
    in it. Returns how many markers were removed.
    """
    removed = 0
    for tag in ("commentRangeStart", "commentRangeEnd"):
        for element in list(root.iter(q(tag))):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
                removed += 1
    for reference in list(root.iter(q("commentReference"))):
        run = reference.getparent()
        if run is not None and run.tag == q("r"):
            parent = run.getparent()
            if parent is not None:
                parent.remove(run)
                removed += 1
    return removed


def remaining_revisions(root: etree._Element) -> list[tuple[str, str | None, str | None]]:
    """Unresolved revisions left in a part, as ``(kind, id, author)``."""
    return [
        (element.tag.split("}")[1], element.get(q("id")), element.get(q("author")))
        for element in root.iter(q("ins"), q("del"))
    ]
