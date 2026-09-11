"""Layer B (scan), part 2: find rule matches and decide which are safe to automate.

The scan report is what management sees first, so its numbers have to survive scrutiny.
Two decisions carry the weight:

**Suppression.** A match covered by one of the rule's exception patterns is not a hit at
all. The exception is matched against the *sentence*, so a quoted citation protects every
deprecated term inside it - which is the behaviour a regulatory reader expects, since the
quote is reproduced verbatim by law.

**Classification.** A hit is ``unambiguous`` only when swapping the term is a plain token
substitution. Anything where the swap would change grammar (an article that no longer
agrees, a plural that would be dropped, a possessive), anything the rulebook marked
``context_required``, anything in a heading whose casing will not map cleanly, and
anything two rules both claim, is ``needs_judgment`` and goes to a human or the LLM.
Getting this boundary wrong in the permissive direction is how an automated pass
introduces errors, so every uncertain case falls to the judgment side.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from termguard.rulebook import CaseKind, Rule, Rulebook
from termguard.walker import Location, WalkedParagraph, walk, walk_bytes

VOWELS = set("aeiouAEIOU")

# Sentence boundary: terminator, closing quote/bracket, then whitespace. Avoids splitting
# on the period inside "21 CFR 820.180" or "e.g." followed by a lowercase word.
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])["”\')\]]*\s+(?=[A-Z(“"])')


@dataclass(frozen=True)
class Hit:
    """One rule match at one location, with everything needed to act on it."""

    location: Location
    rule_id: str
    matched_text: str
    approved_text: str
    span: tuple[int, int]
    occurrence: int
    sentence: str
    paragraph_text: str
    classification: str          # unambiguous | needs_judgment
    reason: str = ""

    @property
    def file(self) -> str:
        return self.location.file

    @property
    def part(self) -> str:
        return self.location.part

    @property
    def needs_judgment(self) -> bool:
        return self.classification == "needs_judgment"

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "rule_id": self.rule_id,
            "classification": self.classification,
            "reason": self.reason,
            "matched_text": self.matched_text,
            "approved_text": self.approved_text,
            "span_start": self.span[0],
            "span_end": self.span[1],
            "occurrence": self.occurrence,
            "sentence": self.sentence,
            "paragraph_text": self.paragraph_text,
            "location": self.location.as_dict(),
        }


# --------------------------------------------------------------------- helpers


def sentence_around(text: str, start: int, end: int) -> str:
    """The sentence containing a span. Falls back to the whole paragraph."""
    if not text:
        return ""
    boundaries = [0]
    for match in _SENTENCE_SPLIT.finditer(text):
        boundaries.append(match.end())
    boundaries.append(len(text))

    for left, right in zip(boundaries, boundaries[1:]):
        if left <= start < right:
            return text[left:right].strip()
    return text.strip()


def _exception_covers(rule: Rule, sentence: str, offset_in_sentence: int, length: int) -> str | None:
    """The exception pattern protecting this span, if any.

    'Covers' means the exception's own match contains the hit. A rule exception for quoted
    CFR text therefore protects every deprecated term inside the quotation, not just the
    first.
    """
    hit_start, hit_end = offset_in_sentence, offset_in_sentence + length
    for pattern in rule.compiled_exceptions():
        for match in pattern.finditer(sentence):
            if match.start() <= hit_start and match.end() >= hit_end:
                return pattern.pattern
    return None


def _preceding_article(text: str, start: int) -> str | None:
    """The indefinite article immediately before a span, if there is one."""
    before = text[:start].rstrip()
    match = re.search(r"\b(an?|An?)$", before)
    return match.group(1) if match else None


def _classify(
    rule: Rule,
    *,
    matched: str,
    replacement: str,
    paragraph: str,
    start: int,
    end: int,
    location: Location,
    overlapping: bool,
) -> tuple[str, str]:
    """Return ``(classification, reason)``.

    Ordered most-specific first so the reason shown to a reviewer is the informative one.
    """
    if overlapping:
        return "needs_judgment", "two rules claim the same span"

    if rule.context_required:
        return "needs_judgment", "rule is context_required"

    # Article agreement: "a side effect" -> "an adverse event".
    article = _preceding_article(paragraph, start)
    if article and matched and replacement:
        if (matched[0] in VOWELS) != (replacement[0] in VOWELS):
            return (
                "needs_judgment",
                f"article '{article}' would no longer agree with '{replacement}'",
            )

    # Plural agreement: "side effects" -> "adverse event" drops the plural.
    matched_plural = matched.lower().endswith("s") and not matched.lower().endswith("ss")
    replacement_plural = replacement.lower().endswith("s") and not replacement.lower().endswith("ss")
    if matched_plural != replacement_plural:
        return (
            "needs_judgment",
            f"number disagrees: '{matched}' is {'plural' if matched_plural else 'singular'}, "
            f"'{replacement}' is {'plural' if replacement_plural else 'singular'}",
        )

    # Possessive: "the physician's record" cannot take a bare swap.
    trailing = paragraph[end : end + 2]
    if trailing.startswith("'s") or trailing.startswith("’s") or matched.endswith("'s"):
        return "needs_judgment", "match is possessive"

    # Headings: casing has to be reproduced by hand when it will not map cleanly.
    if location.is_heading and rule.case is CaseKind.PRESERVE:
        if matched.isupper() and len(matched) > 1:
            return "needs_judgment", "match is in an all-caps heading"
        if " " in matched.strip() and matched.istitle():
            if len(matched.split()) != len(rule.approved.split()):
                return (
                    "needs_judgment",
                    "Title Case heading and the replacement has a different word count",
                )

    return "unambiguous", "plain token substitution"


# ---------------------------------------------------------------------- scanning


def scan_paragraph(para: WalkedParagraph, rulebook: Rulebook) -> list[Hit]:
    """Every hit in one paragraph, with exceptions applied and classification decided."""
    text = para.text
    loc = para.location

    # Pass 1: collect candidate matches from in-scope rules, dropping excepted spans.
    candidates: list[tuple[Rule, re.Match[str]]] = []
    for rule in rulebook:
        if not rule.applies_to(part=loc.part, is_heading=loc.is_heading, in_table=loc.in_table):
            continue
        for pattern in rule.compiled():
            for match in pattern.finditer(text):
                sentence = sentence_around(text, match.start(), match.end())
                offset = _offset_in_sentence(text, sentence, match.start())
                if _exception_covers(rule, sentence, offset, len(match.group(0))) is not None:
                    continue
                candidates.append((rule, match))

    # Pass 2: a span claimed by more than one rule is ambiguous for all claimants.
    spans = Counter((m.start(), m.end()) for _, m in candidates)
    overlaps = _overlapping_spans([(m.start(), m.end()) for _, m in candidates])

    hits: list[Hit] = []
    seen_occurrences: dict[tuple[str, str], int] = defaultdict(int)
    for rule, match in sorted(candidates, key=lambda c: (c[1].start(), c[0].id)):
        matched = match.group(0)
        replacement = rule.render_replacement(matched)
        span = (match.start(), match.end())
        overlapping = spans[span] > 1 or span in overlaps

        classification, reason = _classify(
            rule,
            matched=matched,
            replacement=replacement,
            paragraph=text,
            start=match.start(),
            end=match.end(),
            location=loc,
            overlapping=overlapping,
        )
        key = (rule.id, matched.casefold())
        occurrence = seen_occurrences[key]
        seen_occurrences[key] += 1

        hits.append(
            Hit(
                location=loc,
                rule_id=rule.id,
                matched_text=matched,
                approved_text=replacement,
                span=span,
                occurrence=occurrence,
                sentence=sentence_around(text, match.start(), match.end()),
                paragraph_text=text,
                classification=classification,
                reason=reason,
            )
        )
    return hits


def _offset_in_sentence(paragraph: str, sentence: str, start: int) -> int:
    """Translate a paragraph offset into the enclosing sentence's coordinates."""
    sentence_start = paragraph.find(sentence)
    if sentence_start < 0:
        return start
    return start - sentence_start


def _overlapping_spans(spans: Sequence[tuple[int, int]]) -> set[tuple[int, int]]:
    """Spans that partially overlap another span (identical spans handled separately)."""
    out: set[tuple[int, int]] = set()
    ordered = sorted(set(spans))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b[0] >= a[1]:
                break
            if a != b:
                out.add(a)
                out.add(b)
    return out


def scan_document(path: Path | str, rulebook: Rulebook, *, file_name: str | None = None) -> list[Hit]:
    """Scan one .docx."""
    return [hit for para in walk(path, file_name=file_name) for hit in scan_paragraph(para, rulebook)]


def scan_bytes(data: bytes, file_name: str, rulebook: Rulebook) -> list[Hit]:
    """Scan a document held in memory, e.g. straight from the object store."""
    return [hit for para in walk_bytes(data, file_name) for hit in scan_paragraph(para, rulebook)]


def scan_corpus(directory: Path | str, rulebook: Rulebook) -> list[Hit]:
    """Scan every .docx in a directory, in a stable filename order."""
    directory = Path(directory)
    hits: list[Hit] = []
    for path in sorted(directory.glob("*.docx")):
        hits.extend(scan_document(path, rulebook))
    return hits


# ------------------------------------------------------------------ scan report


@dataclass
class ScanReport:
    """Aggregated scan results. Serializable, because the dashboard reads this shape."""

    hits: list[Hit]
    rulebook_hash: str
    corpus_dir: str = ""
    files_scanned: int = 0

    @property
    def total(self) -> int:
        return len(self.hits)

    @property
    def unambiguous(self) -> int:
        return sum(1 for h in self.hits if h.classification == "unambiguous")

    @property
    def needs_judgment(self) -> int:
        return sum(1 for h in self.hits if h.needs_judgment)

    def by_file(self) -> dict[str, int]:
        return dict(Counter(h.file for h in self.hits))

    def by_part(self) -> dict[str, int]:
        return dict(Counter(h.part for h in self.hits))

    def by_rule(self) -> dict[str, int]:
        return dict(sorted(Counter(h.rule_id for h in self.hits).items()))

    def by_classification(self) -> dict[str, int]:
        return dict(Counter(h.classification for h in self.hits))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rulebook_hash": self.rulebook_hash,
            "corpus_dir": self.corpus_dir,
            "files_scanned": self.files_scanned,
            "total": self.total,
            "unambiguous": self.unambiguous,
            "needs_judgment": self.needs_judgment,
            "by_file": self.by_file(),
            "by_part": self.by_part(),
            "by_rule": self.by_rule(),
            "hits": [h.as_dict() for h in self.hits],
        }


def build_report(
    directory: Path | str, rulebook: Rulebook
) -> ScanReport:
    """Scan a corpus and aggregate it."""
    directory = Path(directory)
    files = sorted(directory.glob("*.docx"))
    hits = [h for path in files for h in scan_document(path, rulebook)]
    return ScanReport(
        hits=hits,
        rulebook_hash=rulebook.hash,
        corpus_dir=str(directory),
        files_scanned=len(files),
    )
