"""Layer A: the terminology rulebook.

A rule is not a find-and-replace pair. The fields that matter are the ones that stop it
from being one: ``exceptions`` (spans that match but must never change, e.g. quoted CFR
text), ``context_required`` (a match alone is not sufficient - a human or the LLM must
judge), ``scope`` (a rule may apply only in body text, not headings), and ``case``
(heading capitalization has to survive a swap).

The rulebook is hashed on load. That hash is recorded on every run and every verification,
so a report can never be silently attributed to a rulebook it was not produced with.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MatchKind(str, Enum):
    WHOLE_WORD = "whole_word"   # token-bounded; "IFU" will not match inside "IFUs"
    PHRASE = "phrase"           # multi-word literal, whitespace-flexible
    REGEX = "regex"             # author-supplied pattern


class CaseKind(str, Enum):
    PRESERVE = "preserve"       # mirror the matched text's casing onto the replacement
    EXACT = "exact"             # match case-sensitively, replace verbatim
    INSENSITIVE = "insensitive" # match case-insensitively, replace verbatim


class Scope(str, Enum):
    BODY = "body"
    HEADINGS = "headings"
    TABLES = "tables"
    HEADERS_FOOTERS = "headers_footers"
    FOOTNOTES = "footnotes"


ALL_SCOPES: tuple[Scope, ...] = tuple(Scope)


class RuleError(ValueError):
    """Raised when a rulebook cannot be loaded or is internally inconsistent."""


class Rule(BaseModel):
    """One terminology rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^R-\d{3}$", description="e.g. R-001")
    deprecated: list[str] = Field(min_length=1)
    approved: str
    match: MatchKind = MatchKind.WHOLE_WORD
    case: CaseKind = CaseKind.PRESERVE
    scope: list[Scope] = Field(default_factory=lambda: list(ALL_SCOPES))
    exceptions: list[str] = Field(default_factory=list)
    context_required: bool = False
    context_note: str = ""
    rationale: str = ""
    owner: str = ""
    effective_date: date | None = None

    @field_validator("deprecated", "exceptions")
    @classmethod
    def _no_blanks(cls, v: list[str]) -> list[str]:
        cleaned = [s for s in (item.strip() for item in v) if s]
        if len(cleaned) != len(v):
            raise ValueError("entries must be non-empty strings")
        return cleaned

    @model_validator(mode="after")
    def _patterns_must_compile(self) -> "Rule":
        for pattern in self.patterns():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"rule {self.id}: bad pattern {pattern!r}: {exc}") from exc
        for exc_pattern in self.exceptions:
            try:
                re.compile(exc_pattern)
            except re.error as exc:
                raise ValueError(f"rule {self.id}: bad exception {exc_pattern!r}: {exc}") from exc
        if self.context_required and not self.context_note:
            raise ValueError(
                f"rule {self.id}: context_required rules must explain themselves in context_note"
            )
        return self

    # -- compiled forms ------------------------------------------------------

    def patterns(self) -> list[str]:
        """Each deprecated term as a regex source string, honoring ``match``."""
        out: list[str] = []
        for term in self.deprecated:
            if self.match is MatchKind.REGEX:
                out.append(term)
            elif self.match is MatchKind.WHOLE_WORD:
                out.append(rf"\b{re.escape(term)}\b")
            else:  # PHRASE - tolerate runs of whitespace between words
                out.append(r"\b" + r"\s+".join(re.escape(w) for w in term.split()) + r"\b")
        return out

    @property
    def flags(self) -> int:
        return 0 if self.case is CaseKind.EXACT else re.IGNORECASE

    def compiled(self) -> list[re.Pattern[str]]:
        return [re.compile(p, self.flags) for p in self.patterns()]

    def compiled_exceptions(self) -> list[re.Pattern[str]]:
        # Exceptions are always case-insensitive: a quoted citation is protected however
        # it is capitalized.
        return [re.compile(p, re.IGNORECASE) for p in self.exceptions]

    def applies_to(self, *, part: str, is_heading: bool, in_table: bool) -> bool:
        """Whether this rule is in scope for a paragraph's structural position."""
        scopes = set(self.scope)
        if part in {"header", "footer"}:
            return Scope.HEADERS_FOOTERS in scopes
        if part in {"footnote", "endnote"}:
            return Scope.FOOTNOTES in scopes
        if is_heading:
            return Scope.HEADINGS in scopes
        if in_table:
            return Scope.TABLES in scopes
        return Scope.BODY in scopes

    def render_replacement(self, matched: str) -> str:
        """The approved term, cased to suit the text it replaces.

        ``preserve`` keeps heading and sentence capitalization intact, which is what stops
        a swap inside a Title Case heading from looking obviously machine-made.
        """
        if self.case is not CaseKind.PRESERVE:
            return self.approved
        if not matched:
            return self.approved
        if matched.isupper() and len(matched) > 1:
            return self.approved.upper()
        # Title Case only propagates from a multi-word match. A single capitalized word is
        # far more often sentence-initial ("Physician shall...") than a Title Case heading
        # fragment, and "Healthcare Provider" mid-sentence reads as machine-made.
        if " " in matched.strip() and matched.istitle():
            return self.approved.title()
        if matched[:1].isupper():
            return self.approved[:1].upper() + self.approved[1:]
        return self.approved


class Rulebook(BaseModel):
    """A validated set of rules, plus the hash that identifies it."""

    model_config = ConfigDict(frozen=True)

    rules: list[Rule]
    version: str = "1"
    source_path: str | None = None
    hash: str = ""

    # -- access --------------------------------------------------------------

    def __iter__(self) -> Iterator[Rule]:  # type: ignore[override]
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)

    def get(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(f"no rule {rule_id!r}")

    @property
    def context_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.context_required]


def compute_hash(rules: Sequence[Rule]) -> str:
    """Stable hash over rule content. Order-independent, so reordering the YAML is a no-op."""
    payload = sorted(
        json.dumps(r.model_dump(mode="json"), sort_keys=True, default=str) for r in rules
    )
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()[:16]


def _check_consistency(rules: Sequence[Rule]) -> None:
    """Reject duplicate ids and deprecated terms claimed by more than one rule."""
    seen_ids: dict[str, int] = {}
    for index, rule in enumerate(rules):
        if rule.id in seen_ids:
            raise RuleError(
                f"duplicate rule id {rule.id!r} at entries {seen_ids[rule.id]} and {index}"
            )
        seen_ids[rule.id] = index

    # Overlap check on literal terms only: two regex rules may legitimately co-occur, and
    # deciding regex intersection in general is not worth the false alarms.
    owner: dict[str, str] = {}
    for rule in rules:
        if rule.match is MatchKind.REGEX:
            continue
        for term in rule.deprecated:
            key = term.casefold()
            if key in owner and owner[key] != rule.id:
                raise RuleError(
                    f"deprecated term {term!r} is claimed by both {owner[key]} and {rule.id}; "
                    "a term must belong to exactly one rule or the scanner cannot attribute a hit"
                )
            owner[key] = rule.id


def load_rulebook(path: Path | str) -> Rulebook:
    """Load, validate and hash a rulebook YAML file."""
    path = Path(path)
    if not path.exists():
        raise RuleError(f"rulebook not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RuleError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "rules" not in raw:
        raise RuleError(f"{path}: expected a mapping with a top-level 'rules' key")

    entries = raw.get("rules") or []
    if not isinstance(entries, list):
        raise RuleError(f"{path}: 'rules' must be a list")

    rules: list[Rule] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuleError(f"{path}: rule at position {index} is not a mapping")
        try:
            rules.append(Rule(**entry))
        except Exception as exc:
            rule_id = entry.get("id", f"<position {index}>")
            raise RuleError(f"{path}: rule {rule_id} is invalid: {exc}") from exc

    _check_consistency(rules)
    return Rulebook(
        rules=rules,
        version=str(raw.get("version", "1")),
        source_path=str(path),
        hash=compute_hash(rules),
    )


def dump_rulebook(rulebook: Rulebook, path: Path | str) -> Path:
    """Write a rulebook back to YAML. Used by the API's PUT /rulebook."""
    path = Path(path)
    payload: dict[str, Any] = {
        "version": rulebook.version,
        "rules": [
            {k: v for k, v in r.model_dump(mode="json").items()
             if v not in (None, "", [], False) or k in {"id", "approved", "deprecated"}}
            for r in rulebook.rules
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100))
    return path
