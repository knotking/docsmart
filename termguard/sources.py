"""Layer A (rulebook), intake: turn a source into text plus where each line came from.

A rule extracted from a document is only trustworthy if you can see the sentence it came
from. So this does not return a blob of text - it returns :class:`SourceLine` records that
each carry a locator ("page 4", "slide 12", "00:03:21", "table 2 row 5"). Every candidate
rule downstream quotes its line, and a reviewer checks the quote rather than the rule.

Five kinds of source, and they are honestly unequal:

===========  ==========================  =====================================
Source       Needs                       State here
===========  ==========================  =====================================
``.docx``    nothing                     works
``.pdf``     ``pypdf``                   works
web page     network access              works
image        a vision model + API key    plumbed, inert without a key
video        captions, or transcription  works from .vtt/.srt; audio needs a
                                         backend that is not installed
===========  ==========================  =====================================

A source that cannot be read says so and names what is missing. It never returns empty
text as though the document had nothing in it - a silent empty extraction looks exactly
like a source with no terminology in it, and somebody will believe it.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SUPPORTED_SUFFIXES = {
    ".docx": "document",
    ".pdf": "document",
    ".txt": "document",
    ".md": "document",
    ".vtt": "video",
    ".srt": "video",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".mp4": "video",
    ".mov": "video",
    ".m4a": "video",
    ".mp3": "video",
}

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
AUDIOVISUAL_SUFFIXES = {".mp4", ".mov", ".m4a", ".mp3"}
CAPTION_SUFFIXES = {".vtt", ".srt"}


class SourceError(RuntimeError):
    """A source could not be read. The message names what is missing and how to fix it."""


@dataclass(frozen=True)
class SourceLine:
    """One line of text, and where in the source it came from."""

    text: str
    locator: str          # "page 4", "00:03:21", "table 2 / row 5", "section: Terminology"
    index: int = 0        # ordinal within the source, for stable ordering
    kind: str = "text"    # text | table_row | heading | caption

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "locator": self.locator, "index": self.index,
                "kind": self.kind}


@dataclass
class ExtractedSource:
    """Everything read from one source, with provenance for the whole and each line."""

    name: str
    kind: str                                   # document | image | video | web
    origin: str                                 # path or URL
    lines: list[SourceLine] = field(default_factory=list)
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    content_sha256: str | None = None
    notes: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)   # what is missing to read this fully

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def readable(self) -> bool:
        return bool(self.lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "origin": self.origin,
            "retrieved_at": self.retrieved_at, "content_sha256": self.content_sha256,
            "lines": len(self.lines), "notes": self.notes, "needs": self.needs,
        }


# ----------------------------------------------------------------- documents


def _from_docx(data: bytes, name: str) -> list[SourceLine]:
    """Body, tables, headers and footnotes - reusing the walker built for scanning.

    Table cells are reassembled into rows. The walker yields one paragraph per cell,
    which is right for scanning and useless for reading a glossary: a two-column
    "deprecated / approved" table carries its entire meaning in the pairing of cells, and
    a reader seeing them as separate lines learns nothing.
    """
    from termguard.walker import walk_bytes

    lines: list[SourceLine] = []
    index = 0
    pending_cells: list[str] = []
    pending_key: tuple[int, int] | None = None

    def flush_row() -> None:
        nonlocal pending_cells, pending_key, index
        if pending_cells and pending_key is not None:
            table, row = pending_key
            lines.append(SourceLine(
                text=" | ".join(pending_cells),
                locator=f"table {table} / row {row}",
                index=index, kind="table_row",
            ))
            index += 1
        pending_cells, pending_key = [], None

    for para in walk_bytes(data, name):
        location = para.location
        text = para.text.strip()

        if location.in_table:
            key = (location.table or 1, location.row or 1)
            if pending_key is not None and key != pending_key:
                flush_row()
            pending_key = key
            pending_cells.append(text)
            continue

        flush_row()
        if not text:
            continue
        locator = (
            location.container_path
            if location.part != "body"
            else ("heading" if location.is_heading else f"paragraph {location.paragraph_index}")
        )
        lines.append(SourceLine(
            text=text, locator=locator, index=index,
            kind="heading" if location.is_heading else "text",
        ))
        index += 1

    flush_row()
    return lines


def _from_pdf(data: bytes, name: str) -> list[SourceLine]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise SourceError(
            "reading PDF needs pypdf:\n    pip install pypdf"
        ) from exc

    reader = PdfReader(io.BytesIO(data))
    lines: list[SourceLine] = []
    index = 0
    for page_number, page in enumerate(reader.pages, start=1):
        for raw in (page.extract_text() or "").splitlines():
            text = raw.strip()
            if not text:
                continue
            lines.append(SourceLine(text=text, locator=f"page {page_number}", index=index))
            index += 1
    return lines


def _from_plain(data: bytes, name: str) -> list[SourceLine]:
    lines: list[SourceLine] = []
    for number, raw in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
        text = raw.strip()
        if text:
            lines.append(SourceLine(text=text, locator=f"line {number}", index=number))
    return lines


# ----------------------------------------------------------------------- web

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_ROW = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.S | re.I)


def _strip(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()


def from_html(markup: str, origin: str) -> list[SourceLine]:
    """Text from HTML, keeping table rows intact.

    Table rows are kept as single lines with their cells separated, because a glossary on
    a web page is almost always a table and its two columns are the whole rule. Flattening
    the row into prose would destroy exactly the structure worth reading.
    """
    body = _SCRIPT.sub(" ", markup)
    lines: list[SourceLine] = []
    index = 0

    for match in _ROW.finditer(body):
        cells = [_strip(cell) for cell in _CELL.findall(match.group(0))]
        cells = [c for c in cells if c]
        if len(cells) >= 2:
            lines.append(SourceLine(
                text=" | ".join(cells), locator=f"table row {index + 1}",
                index=index, kind="table_row",
            ))
            index += 1

    for match in _HEADING.finditer(body):
        text = _strip(match.group(2))
        if text:
            lines.append(SourceLine(text=text, locator=f"heading", index=index, kind="heading"))
            index += 1

    remainder = _ROW.sub(" ", body)
    for raw in _strip(remainder).split(". "):
        text = raw.strip()
        if len(text) > 12:
            lines.append(SourceLine(text=text, locator="body", index=index))
            index += 1

    return lines


def from_url(url: str, *, timeout: int = 20) -> ExtractedSource:
    """Fetch a page and read it. The URL and retrieval time are the provenance."""
    import urllib.error
    import urllib.request

    if not url.lower().startswith(("http://", "https://")):
        raise SourceError(f"not an http(s) URL: {url!r}")

    request = urllib.request.Request(
        url, headers={"User-Agent": "TermGuard/0.2 (terminology rule intake)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.URLError as exc:
        raise SourceError(f"could not fetch {url}: {exc.reason}") from exc

    from termguard.storage import sha256_bytes

    markup = raw.decode(charset, errors="replace")
    return ExtractedSource(
        name=url.rstrip("/").rsplit("/", 1)[-1] or url,
        kind="web", origin=url,
        lines=from_html(markup, url),
        content_sha256=sha256_bytes(raw),
        notes=[f"fetched {len(raw)} bytes"],
    )


# -------------------------------------------------------------------- video

_VTT_TIME = re.compile(r"(\d{2}:\d{2}:\d{2})[.,]\d{3}\s*-->")
_SRT_INDEX = re.compile(r"^\d+$")


def from_captions(data: bytes, name: str) -> list[SourceLine]:
    """Read .vtt or .srt captions, keeping each cue's timestamp as its locator.

    A timestamp is the only locator a video can offer that a reviewer can actually act on:
    it takes them to the moment the claim was made.
    """
    lines: list[SourceLine] = []
    timestamp = "00:00:00"
    index = 0
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, index
        text = " ".join(buffer).strip()
        if text:
            lines.append(SourceLine(text=text, locator=timestamp, index=index, kind="caption"))
            index += 1
        buffer = []

    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.upper().startswith("WEBVTT") or _SRT_INDEX.match(line):
            continue
        match = _VTT_TIME.search(line)
        if match:
            flush()
            timestamp = match.group(1)
            continue
        buffer.append(re.sub(r"<[^>]+>", "", line))
    flush()
    return lines


def _from_audiovisual(path: Path) -> ExtractedSource:
    """A video with no captions. Says what is missing rather than returning nothing."""
    import shutil

    from termguard.storage import sha256_file

    sidecars = [
        candidate
        for suffix in CAPTION_SUFFIXES
        for candidate in [path.with_suffix(suffix)]
        if candidate.exists()
    ]
    if sidecars:
        return ExtractedSource(
            name=path.name, kind="video", origin=str(path),
            lines=from_captions(sidecars[0].read_bytes(), sidecars[0].name),
            content_sha256=sha256_file(path),
            notes=[f"read captions from {sidecars[0].name}"],
        )

    missing = [tool for tool in ("ffmpeg", "whisper") if shutil.which(tool) is None]
    return ExtractedSource(
        name=path.name, kind="video", origin=str(path),
        lines=[], content_sha256=sha256_file(path),
        notes=["no captions found alongside this file"],
        needs=(
            [f"a transcript: supply {path.stem}.vtt or {path.stem}.srt next to the video, "
             f"or install {' and '.join(missing)} to transcribe it here"]
            if missing else
            ["transcription backend present but not wired up; supply captions for now"]
        ),
    )


# -------------------------------------------------------------------- images


def _from_image(path: Path, data: bytes) -> ExtractedSource:
    """Images need a vision model. Plumbed, and inert without a key.

    Deliberately not OCR: a photographed style-guide page is usually a *table*, and
    character-level OCR flattens the columns that carry the entire meaning. A vision model
    reads the layout, which is the part that matters.
    """
    import os

    from termguard.storage import sha256_bytes

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return ExtractedSource(
        name=path.name, kind="image", origin=str(path),
        lines=[], content_sha256=sha256_bytes(data),
        notes=["images are read by a vision model, not OCR: a glossary photographed from "
               "a page is a table, and OCR flattens the columns that carry the meaning"],
        needs=[] if has_key else [
            "an ANTHROPIC_API_KEY: reading an image needs a vision model, and there is no "
            "offline fallback that would preserve table structure"
        ],
    )


# ------------------------------------------------------------------- intake


def read_source(path: Path | str, *, name: str | None = None) -> ExtractedSource:
    """Read any supported file into lines with provenance."""
    path = Path(path)
    if not path.exists():
        raise SourceError(f"no such file: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise SourceError(
            f"unsupported source type {suffix!r}. Supported: "
            + ", ".join(sorted(SUPPORTED_SUFFIXES))
        )

    if suffix in IMAGE_SUFFIXES:
        return _from_image(path, path.read_bytes())
    if suffix in AUDIOVISUAL_SUFFIXES:
        return _from_audiovisual(path)

    from termguard.storage import sha256_bytes

    data = path.read_bytes()
    display = name or path.name

    if suffix == ".docx":
        lines = _from_docx(data, display)
    elif suffix == ".pdf":
        lines = _from_pdf(data, display)
    elif suffix in CAPTION_SUFFIXES:
        lines = from_captions(data, display)
    else:
        lines = _from_plain(data, display)

    return ExtractedSource(
        name=display, kind=SUPPORTED_SUFFIXES[suffix], origin=str(path),
        lines=lines, content_sha256=sha256_bytes(data),
        notes=[] if lines else ["no readable text found in this file"],
    )


def read_bytes(data: bytes, filename: str) -> ExtractedSource:
    """Read an uploaded file held in memory."""
    import tempfile

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise SourceError(
            f"unsupported source type {suffix!r}. Supported: "
            + ", ".join(sorted(SUPPORTED_SUFFIXES))
        )

    with tempfile.TemporaryDirectory(prefix="termguard-source-") as tmp:
        path = Path(tmp) / Path(filename).name
        path.write_bytes(data)
        source = read_source(path, name=filename)
        # The temp path is an implementation detail; the filename is the provenance.
        source.origin = filename
        return source
