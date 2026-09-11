"""Layer A (rulebook), intake: turn source text into candidate rules.

Patterns first, a model only for what patterns cannot reach. That ordering is not
economy - a pattern match is *quotable*: it points at the exact line that produced the
rule, it behaves identically every run, and a reviewer can check it in a second. A model
reading prose is more capable and strictly less checkable, so it handles the remainder.

**The direction is the dangerous part.** "Use mL, not ml" and "ml is used, not mL" differ
by word order and produce opposite rules, and a reversed rule does not fail loudly - it
quietly rewrites correct text into wrong text across a corpus. So every pattern here
declares which capture group is the deprecated term and which is the approved one, and no
candidate is ever emitted without the source line attached for a human to check against.

Nothing extracted here enters the rulebook. Candidates are reviewed, and the reviewer is
looking at the quote, not at the rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from termguard.rulebook import CaseKind, MatchKind, Rule, Rulebook
from termguard.sources import ExtractedSource, SourceLine

# Column headings that mark which side of a glossary table is which. A table whose
# headings match neither list is not read as a glossary: guessing the direction from
# column order is exactly the mistake that produces reversed rules.
DEPRECATED_HEADINGS = {
    "deprecated", "avoid", "do not use", "dont use", "don't use", "old", "old term",
    "obsolete", "incorrect", "wrong", "non-preferred", "not preferred", "legacy",
    "former", "instead of", "replace", "from", "banned", "discouraged",
}
APPROVED_HEADINGS = {
    "approved", "use", "use instead", "preferred", "new", "new term", "correct",
    "recommended", "standard", "to", "replacement", "prefer", "required", "accepted",
}

# Words that are never a terminology term on their own.
_NOISE = {
    "", "n/a", "na", "none", "-", "—", "tbd", "see below", "as above", "term", "notes",
}

_MAX_TERM_WORDS = 6
_MAX_TERM_CHARS = 60


@dataclass(frozen=True)
class Pattern:
    """One prose form, with its direction stated rather than inferred."""

    name: str
    regex: re.Pattern[str]
    deprecated_group: int
    approved_group: int
    confidence: float
    example: str


def _p(name: str, source: str, deprecated: int, approved: int,
       confidence: float, example: str) -> Pattern:
    return Pattern(name, re.compile(source, re.I), deprecated, approved, confidence, example)


# Words a terminology term does not cross. Without this the capture runs on past the
# term and into the rest of the sentence - "medication library throughout", "shall in
# requirement statements" - producing rules whose deprecated side is a clause. Every one
# of those would then fail to match anything, so the rule looks fine and silently does
# nothing, which is worse than an obvious error.
# Deliberately excludes "for", "of", "with" and "by": real terms contain them
# ("Instructions for Use", "certificate of conformity"), and treating them as boundaries
# truncates the term into something that matches nothing - a rule that looks correct and
# silently does nothing, which is worse than one that looks obviously wrong.
_STOP = (
    r"in|on|at|to|throughout|when|whenever|except|unless|within|across|during|"
    r"per|and|or|but|if|because|since|while|after|before|that|which|"
    r"wherever|only|always|never|instead|rather"
)
_WORD = rf"(?!(?:{_STOP})\b)[\w./-]+"
# Up to five words, none of them a stop word, optionally quoted.
_T = rf"[\"'“‘]?({_WORD}(?:\s+{_WORD}){{0,4}})[\"'”’]?"

# Lead-ins that are commentary, not part of the term.
_LEAD_IN = re.compile(
    r"^(?:the\s+)?(?:term|word|phrase|expression|abbreviation|acronym)\s+", re.I
)

PATTERNS: tuple[Pattern, ...] = (
    _p("use-not", rf"\buse\s+{_T}\s*,?\s+not\s+{_T}", 2, 1, 0.9,
       "Use mL, not ml."),
    _p("use-instead-of", rf"\buse\s+{_T}\s+instead\s+of\s+{_T}", 2, 1, 0.9,
       "Use administration set instead of infusion set."),
    _p("use-rather-than", rf"\buse\s+{_T}\s+rather\s+than\s+{_T}", 2, 1, 0.9,
       "Use healthcare provider rather than physician."),
    _p("not-but", rf"\bnot\s+{_T}\s*[,;]?\s+but\s+{_T}", 1, 2, 0.75,
       "Not physician, but healthcare provider."),
    _p("do-not-use-use", rf"\bdo\s+not\s+use\s+{_T}\s*[;,.]\s*use\s+{_T}", 1, 2, 0.9,
       "Do not use IFU; use Instructions for Use."),
    _p("avoid-use", rf"\bavoid\s+{_T}\s*[;,.]\s*(?:use|prefer)\s+{_T}", 1, 2, 0.85,
       "Avoid side effect; use adverse event."),
    _p("replace-with", rf"\breplace\s+{_T}\s+with\s+{_T}", 1, 2, 0.85,
       "Replace drug library with medication library."),
    _p("deprecated-use", rf"{_T}\s+(?:is|has been|was)\s+deprecated\s*[;,.]?\s*use\s+{_T}",
       1, 2, 0.9, "Meridian Pump 2 is deprecated; use Meridian Infusion System."),
    _p("replaced-by", rf"{_T}\s+(?:has been|was|is)\s+replaced\s+by\s+{_T}", 1, 2, 0.85,
       "Error message has been replaced by alarm condition."),
    _p("no-longer-use", rf"\b(?:no longer|never)\s+use\s+{_T}\s*[;,.]\s*use\s+{_T}", 1, 2, 0.85,
       "No longer use nurse; use clinician."),
    _p("prefer-over", rf"\bprefer\s+{_T}\s+(?:over|to)\s+{_T}", 2, 1, 0.85,
       "Prefer must over shall."),
    _p("should-be", rf"{_T}\s+should\s+(?:be|read)\s+{_T}", 1, 2, 0.6,
       "Labelling should be labeling."),
)


@dataclass
class Candidate:
    """A proposed rule, and the line it came from.

    ``quote`` is the point. A reviewer checks the sentence and decides whether the rule
    reads it correctly; they are not being asked to trust the extractor.
    """

    deprecated: str
    approved: str
    quote: str
    locator: str
    source_name: str
    method: str                       # the pattern name, or "model"
    confidence: float = 0.5
    note: str = ""
    warnings: list[str] = field(default_factory=list)
    suggested: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.deprecated.casefold()}->{self.approved.casefold()}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "deprecated": self.deprecated, "approved": self.approved,
            "quote": self.quote, "locator": self.locator, "source": self.source_name,
            "method": self.method, "confidence": round(self.confidence, 2),
            "note": self.note, "warnings": self.warnings, "suggested": self.suggested,
        }

    def to_rule(self, rule_id: str, *, owner: str = "", rationale: str | None = None) -> Rule:
        """Build a real rule. Conservative defaults; the reviewer widens them if needed."""
        multiword = " " in self.deprecated.strip()
        case_only = (
            self.deprecated.casefold() == self.approved.casefold()
            and self.deprecated != self.approved
        )
        return Rule(
            id=rule_id,
            deprecated=[self.deprecated],
            approved=self.approved,
            match=MatchKind.PHRASE if multiword else MatchKind.WHOLE_WORD,
            # A case-only rule must replace verbatim, or preserving the source's casing
            # reproduces the very spelling the rule exists to fix.
            case=CaseKind.EXACT if case_only else CaseKind.PRESERVE,
            context_required=bool(self.suggested.get("context_required")),
            context_note=self.suggested.get("context_note", ""),
            rationale=rationale or self.note or f"From {self.source_name} ({self.locator}).",
            owner=owner,
        )


# ------------------------------------------------------------------ cleaning


def _clean_term(raw: str) -> str:
    term = re.sub(r"\s+", " ", raw or "").strip().strip("\"'“”‘’")
    term = _LEAD_IN.sub("", term)          # "the term X" -> "X"
    term = term.strip(" .,;:()[]")
    return term


def _plausible_term(term: str) -> bool:
    """Reject things that are not terminology: sentences, empties, table filler."""
    if not term or term.casefold() in _NOISE:
        return False
    if len(term) > _MAX_TERM_CHARS or len(term.split()) > _MAX_TERM_WORDS:
        return False
    if not any(ch.isalpha() for ch in term):
        return False
    # A term ending in a sentence terminator is a swallowed clause, not a term.
    return not term.endswith((".", "!", "?"))


# -------------------------------------------------------------------- tables


def _split_row(text: str) -> list[str]:
    for separator in (" | ", "\t", " :: "):
        if separator in text:
            return [_clean_term(cell) for cell in text.split(separator)]
    return []


def _heading_direction(cells: Sequence[str]) -> tuple[int, int] | None:
    """Find which column is deprecated and which is approved, from the headings.

    Returns None when the headings do not say. Column order is *not* a fallback: a
    glossary whose direction has to be guessed is exactly the case that produces reversed
    rules, and reversed rules break correct text.
    """
    deprecated_at = approved_at = None
    for index, cell in enumerate(cells):
        key = cell.casefold().strip(" :*")
        if key in DEPRECATED_HEADINGS and deprecated_at is None:
            deprecated_at = index
        elif key in APPROVED_HEADINGS and approved_at is None:
            approved_at = index
    if deprecated_at is None or approved_at is None or deprecated_at == approved_at:
        return None
    return deprecated_at, approved_at


def extract_from_tables(source: ExtractedSource) -> list[Candidate]:
    """Read glossary tables, using the heading row to fix the direction."""
    candidates: list[Candidate] = []
    direction: tuple[int, int] | None = None
    heading_line: SourceLine | None = None

    for line in source.lines:
        cells = _split_row(line.text)
        if len(cells) < 2:
            continue

        found = _heading_direction(cells)
        if found is not None:
            direction, heading_line = found, line
            continue
        if direction is None:
            continue

        deprecated_at, approved_at = direction
        if max(deprecated_at, approved_at) >= len(cells):
            continue
        deprecated = _clean_term(cells[deprecated_at])
        approved = _clean_term(cells[approved_at])
        if not (_plausible_term(deprecated) and _plausible_term(approved)):
            continue
        if deprecated.casefold() == approved.casefold() and deprecated == approved:
            continue

        note = ""
        remaining = [c for i, c in enumerate(cells) if i not in direction and c]
        if remaining:
            note = remaining[0][:220]

        candidates.append(Candidate(
            deprecated=deprecated, approved=approved, quote=line.text,
            locator=line.locator, source_name=source.name,
            method="glossary-table", confidence=0.95, note=note,
            suggested=_suggest_from_note(note),
        ))
    return candidates


# -------------------------------------------------------------------- prose


def extract_from_prose(source: ExtractedSource) -> list[Candidate]:
    """Apply the declared prose patterns, keeping the sentence each came from."""
    candidates: list[Candidate] = []
    for line in source.lines:
        if line.kind == "table_row":
            continue  # tables are read by the table reader, with their direction known
        for pattern in PATTERNS:
            for match in pattern.regex.finditer(line.text):
                deprecated = _clean_term(match.group(pattern.deprecated_group))
                approved = _clean_term(match.group(pattern.approved_group))
                if not (_plausible_term(deprecated) and _plausible_term(approved)):
                    continue
                if deprecated.casefold() == approved.casefold() and deprecated == approved:
                    continue
                candidates.append(Candidate(
                    deprecated=deprecated, approved=approved, quote=line.text.strip(),
                    locator=line.locator, source_name=source.name,
                    method=pattern.name, confidence=pattern.confidence,
                    suggested=_suggest_from_note(line.text),
                ))
    return candidates


_CONTEXT_MARKERS = ("context", "depends", "except", "only", "unless", "sometimes",
                    "varies", "audience", "patient-facing", "case by case")


def _suggest_from_note(text: str) -> dict[str, Any]:
    """Flag a candidate as needing judgement when its own source says it depends."""
    lowered = (text or "").casefold()
    if any(marker in lowered for marker in _CONTEXT_MARKERS):
        return {"context_required": True, "context_note": (text or "").strip()[:400]}
    return {}


# ---------------------------------------------------------------- assembly


def _dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Collapse repeats, keeping the best-evidenced one and counting the rest."""
    best: dict[str, Candidate] = {}
    seen: dict[str, int] = {}
    for candidate in candidates:
        seen[candidate.key] = seen.get(candidate.key, 0) + 1
        existing = best.get(candidate.key)
        if existing is None or candidate.confidence > existing.confidence:
            best[candidate.key] = candidate
    for key, candidate in best.items():
        if seen[key] > 1:
            candidate.note = (
                f"{candidate.note} (stated {seen[key]} times in this source)".strip()
            )
    return list(best.values())


def _check_against_rulebook(candidates: Sequence[Candidate], rulebook: Rulebook | None) -> None:
    """Warn about candidates that clash with rules already in force.

    A candidate that contradicts an existing rule is the most dangerous kind: accepting it
    would leave the rulebook telling the scanner two different things about one term.
    """
    if rulebook is None:
        return
    owners: dict[str, tuple[str, str]] = {}
    for rule in rulebook:
        for term in rule.deprecated:
            owners[term.casefold()] = (rule.id, rule.approved)
    # Case-sensitive: a case-only rule ("ml" -> "mL") has a deprecated and an approved
    # term that are equal under casefold, and matching case-insensitively reports the
    # rule as conflicting with itself.
    approved_terms = {rule.approved: rule.id for rule in rulebook}

    for candidate in candidates:
        existing = owners.get(candidate.deprecated.casefold())
        if existing is not None:
            rule_id, approved = existing
            if approved.casefold() == candidate.approved.casefold():
                candidate.warnings.append(f"already covered by {rule_id}")
            else:
                candidate.warnings.append(
                    f"CONFLICT: {rule_id} maps this term to {approved!r}, "
                    f"this source says {candidate.approved!r}"
                )
        # A term this source wants to deprecate that another rule treats as correct.
        holder = approved_terms.get(candidate.deprecated)
        if holder is not None:
            candidate.warnings.append(
                f"CONFLICT: {holder} treats {candidate.deprecated!r} as the approved term"
            )
        replacement_owner = owners.get(candidate.approved.casefold())
        if replacement_owner is not None and replacement_owner[1] != candidate.approved:
            candidate.warnings.append(
                f"the replacement {candidate.approved!r} is itself deprecated by "
                f"{replacement_owner[0]}"
            )


def extract(
    source: ExtractedSource, *, rulebook: Rulebook | None = None
) -> list[Candidate]:
    """Every candidate rule a source yields, deduped and checked against the rulebook.

    Ordered by how well evidenced each is, so a reviewer works down from the glossary
    tables - which are near-certain - into the prose, which is not.
    """
    candidates = _dedupe([*extract_from_tables(source), *extract_from_prose(source)])
    _check_against_rulebook(candidates, rulebook)
    candidates.sort(key=lambda c: (-c.confidence, c.deprecated.casefold()))
    return candidates


def next_rule_id(rulebook: Rulebook | None, taken: Iterable[str] = ()) -> str:
    """The next free R-### id."""
    used = {rule.id for rule in (rulebook or [])} | set(taken)
    number = 1
    while f"R-{number:03d}" in used:
        number += 1
    return f"R-{number:03d}"
