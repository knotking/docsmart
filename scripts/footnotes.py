"""Raw-OOXML footnote injection.

python-docx 1.2.0 has no footnote-authoring API (verified: ``Paragraph.add_footnote``
does not exist), so ``make_corpus.py`` needs this to plant violations in footnotes -
one of the places manual review reliably misses.

Approach: let python-docx write the document normally with a text marker where each
footnote reference belongs, then post-process the .docx zip to

1. build ``word/footnotes.xml`` (with the separator / continuationSeparator entries Word
   expects before any real note),
2. declare it in ``word/_rels/document.xml.rels`` and ``[Content_Types].xml``,
3. replace each marker run in ``word/document.xml`` with a real ``w:footnoteReference``.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W, "r": R}

FOOTNOTES_CT = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.footnotes+xml"
)
FOOTNOTES_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
)


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _footnotes_xml(notes: dict[int, str]) -> bytes:
    """Build word/footnotes.xml. Ids -1 and 0 are reserved by the spec."""
    root = etree.Element(_q("footnotes"), nsmap={"w": W, "r": R})

    for fid, kind in ((-1, "separator"), (0, "continuationSeparator")):
        fn = etree.SubElement(root, _q("footnote"))
        fn.set(_q("type"), kind)
        fn.set(_q("id"), str(fid))
        p = etree.SubElement(fn, _q("p"))
        r = etree.SubElement(p, _q("r"))
        etree.SubElement(r, _q(kind if kind == "separator" else "continuationSeparator"))

    for fid, text in sorted(notes.items()):
        fn = etree.SubElement(root, _q("footnote"))
        fn.set(_q("id"), str(fid))
        p = etree.SubElement(fn, _q("p"))
        ppr = etree.SubElement(p, _q("pPr"))
        style = etree.SubElement(ppr, _q("pStyle"))
        style.set(_q("val"), "FootnoteText")

        ref_run = etree.SubElement(p, _q("r"))
        rpr = etree.SubElement(ref_run, _q("rPr"))
        rstyle = etree.SubElement(rpr, _q("rStyle"))
        rstyle.set(_q("val"), "FootnoteReference")
        etree.SubElement(ref_run, _q("footnoteRef"))

        text_run = etree.SubElement(p, _q("r"))
        t = etree.SubElement(text_run, _q("t"))
        t.text = f" {text}"
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _make_reference_run(fid: int) -> etree._Element:
    run = etree.Element(_q("r"))
    rpr = etree.SubElement(run, _q("rPr"))
    rstyle = etree.SubElement(rpr, _q("rStyle"))
    rstyle.set(_q("val"), "FootnoteReference")
    ref = etree.SubElement(run, _q("footnoteReference"))
    ref.set(_q("id"), str(fid))
    return run


def inject_footnotes(path: Path, markers: dict[str, str]) -> int:
    """Replace each marker string in ``path`` with a real footnote carrying its text.

    ``markers`` maps a unique marker found in the body (e.g. ``"[[FN1]]"``) to the
    footnote's text. Returns the number of footnotes written.
    """
    if not markers:
        return 0

    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        parts = {n: zf.read(n) for n in zf.namelist()}

    doc = etree.fromstring(parts["word/document.xml"])

    notes: dict[int, str] = {}
    next_id = 1
    for marker, note_text in markers.items():
        target = None
        for t in doc.iter(_q("t")):
            if t.text and marker in t.text:
                target = t
                break
        if target is None:
            raise ValueError(f"marker {marker!r} not found in document body")

        # Drop the marker text, then insert a reference run after its run.
        target.text = target.text.replace(marker, "")
        run = target.getparent()
        while run is not None and run.tag != _q("r"):
            run = run.getparent()
        if run is None:
            raise ValueError(f"marker {marker!r} is not inside a run")
        run.addnext(_make_reference_run(next_id))
        notes[next_id] = note_text
        next_id += 1

    parts["word/document.xml"] = etree.tostring(
        doc, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    parts["word/footnotes.xml"] = _footnotes_xml(notes)

    # Relationship: document.xml -> footnotes.xml
    rels = etree.fromstring(parts["word/_rels/document.xml.rels"])
    if not any(rel.get("Type") == FOOTNOTES_REL for rel in rels):
        used = {rel.get("Id", "") for rel in rels}
        rid, n = "rId100", 100
        while rid in used:
            n += 1
            rid = f"rId{n}"
        rel = etree.SubElement(rels, f"{{{PR}}}Relationship")
        rel.set("Id", rid)
        rel.set("Type", FOOTNOTES_REL)
        rel.set("Target", "footnotes.xml")
        parts["word/_rels/document.xml.rels"] = etree.tostring(
            rels, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    # Content type override
    types = etree.fromstring(parts["[Content_Types].xml"])
    if not any(o.get("PartName") == "/word/footnotes.xml" for o in types):
        override = etree.SubElement(types, f"{{{CT}}}Override")
        override.set("PartName", "/word/footnotes.xml")
        override.set("ContentType", FOOTNOTES_CT)
        parts["[Content_Types].xml"] = etree.tostring(
            types, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    tmp = path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in parts.items():
            out.writestr(name, data)
    shutil.move(str(tmp), str(path))
    return len(notes)
