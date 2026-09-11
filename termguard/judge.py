"""Layer C: the LLM judgment step, and the containment that makes it defensible.

Constraint 3. The model is invoked only on hits the scanner classified as
``needs_judgment``, it sees one sentence plus its paragraph and never the document, and
its output is validated by code before it can become a redline. That containment - not the
model's good behaviour - is the compliance argument, so every check here is written to
fail closed.

Three validations, in order:

1. **Strict JSON.** The response must parse and carry the three expected fields. A
   malformed response escalates; it never degrades into a guess.
2. **Over-edit rejection.** The revised sentence is token-diffed against the original.
   Any token changed outside the flagged span plus a two-token margin is an over-edit and
   is rejected, however sensible the rewrite looks. This is the check that stops a model
   from quietly "improving" a regulated sentence.
3. **Term presence.** A ``change`` decision whose revised sentence does not contain the
   approved term is incoherent, and is rejected.

A rejected response becomes ``escalate`` with a reason, which routes it to a human. It is
never silently dropped and never applied.

On determinism: the original design called for ``temperature=0``. Temperature was removed
from the API for current models (Opus 5, Sonnet 5, Opus 4.7/4.8 reject it with a 400), so
reproducibility is obtained instead from a constrained JSON schema via structured outputs,
low reasoning effort, and a response cache keyed by
``(sentence, rule id, prompt hash)`` - which also makes re-runs free.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence

from termguard.config import Settings, get_settings
from termguard.rulebook import Rule, Rulebook
from termguard.scanner import Hit

PROMPT_VERSION = "2026-09-10.1"
TOKEN_MARGIN = 2

DECISIONS = ("change", "keep", "escalate")

SYSTEM_PROMPT = """\
You are a terminology reviewer for a regulated medical-device document set (FDA-facing).
You are editing ONE SENTENCE of a controlled document. You are not improving the writing.

The single rule under consideration:

  Rule id:        {rule_id}
  Deprecated:     {deprecated}
  Approved:       {approved}
  Rationale:      {rationale}
  When to keep:   {context_note}

The reviewer has flagged one span of the sentence. Decide only about that span.

Decide one of:
  "change"   - the flagged span must be replaced with the approved term
  "keep"     - the deprecated term is correct in this context; leave the sentence alone
  "escalate" - you cannot decide from the context given

Hard constraints on your revised sentence:
- Change ONLY the flagged span, plus the minimum grammatical agreement the swap forces
  (an indefinite article a/an, a plural, a possessive, or the capitalization of the
  replacement itself).
- Do not reword, reorder, shorten, clarify, or correct anything else, even if it is
  wrong. Text outside the flagged span is out of scope and edits to it will be rejected.
- If the decision is "keep" or "escalate", return the original sentence unchanged.
- The justification is one sentence explaining the decision from the context.
"""

USER_PROMPT = """\
Document part: {part}
Location: {location}

Paragraph context:
{paragraph}

Sentence to edit:
{sentence}

Flagged span: characters {span_start}-{span_end} of the sentence, which is: {matched!r}
Approved replacement for that span: {approved!r}
Why this reached you: {reason}
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "revised_sentence": {"type": "string"},
        "justification": {"type": "string"},
    },
    "required": ["decision", "revised_sentence", "justification"],
    "additionalProperties": False,
}


class JudgeError(RuntimeError):
    """Raised when the judge cannot be run at all (not when a response is rejected)."""


@dataclass
class JudgeOutcome:
    """The validated result of one judgment, with full provenance."""

    hit: Hit
    decision: str                      # change | keep | escalate
    revised_sentence: str
    justification: str
    model: str
    prompt_version: str
    prompt_hash: str
    request_id: str | None = None
    latency_ms: int = 0
    rejected_reason: str | None = None  # set when the model's answer was overridden
    raw_decision: str | None = None     # what the model said before validation
    cached: bool = False

    @property
    def accepted_change(self) -> bool:
        return self.decision == "change"

    @property
    def was_rejected(self) -> bool:
        return self.rejected_reason is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.hit.rule_id,
            "decision": self.decision,
            "raw_decision": self.raw_decision,
            "revised_sentence": self.revised_sentence,
            "justification": self.justification,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "prompt_hash": self.prompt_hash,
            "request_id": self.request_id,
            "latency_ms": self.latency_ms,
            "rejected_reason": self.rejected_reason,
            "cached": self.cached,
        }


# ------------------------------------------------------------------ prompting


def build_prompts(hit: Hit, rule: Rule) -> tuple[str, str]:
    """The exact system and user prompts sent for a hit."""
    sentence = hit.sentence or hit.paragraph_text
    span_start = sentence.find(hit.matched_text)
    if span_start < 0:
        span_start = 0
    system = SYSTEM_PROMPT.format(
        rule_id=rule.id,
        deprecated=", ".join(rule.deprecated),
        approved=rule.approved,
        rationale=" ".join(rule.rationale.split()) or "(none recorded)",
        context_note=" ".join(rule.context_note.split()) or "(no context guidance)",
    )
    user = USER_PROMPT.format(
        part=_describe_part(hit),
        location=hit.location.describe(),
        paragraph=hit.paragraph_text,
        sentence=sentence,
        span_start=span_start,
        span_end=span_start + len(hit.matched_text),
        matched=hit.matched_text,
        approved=hit.approved_text,
        reason=hit.reason or "flagged for judgment",
    )
    return system, user


def _describe_part(hit: Hit) -> str:
    """Tell the model where it is - a patient-facing heading is decisive for R-002."""
    location = hit.location
    if location.is_heading:
        return f"{location.part} section heading: {hit.paragraph_text[:60]!r}"
    if location.in_table:
        return f"table cell ({location.container_path})"
    return location.part


def prompt_hash(system: str, user_template: str = USER_PROMPT) -> str:
    """Identifies the prompt that produced a decision. Recorded on every change."""
    payload = f"{PROMPT_VERSION}\n{system}\n{user_template}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ validation


_TOKEN = re.compile(r"\w+|[^\w\s]")


def tokenize(text: str) -> list[tuple[str, int, int]]:
    """Tokens with their character offsets."""
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def _span_token_range(tokens: Sequence[tuple[str, int, int]], start: int, end: int) -> tuple[int, int]:
    """Indices of the tokens overlapping a character span."""
    indices = [i for i, (_, s, e) in enumerate(tokens) if e > start and s < end]
    if not indices:
        return 0, len(tokens)
    return indices[0], indices[-1]


def detect_over_edit(
    original: str, revised: str, span: tuple[int, int], margin: int = TOKEN_MARGIN
) -> str | None:
    """Return a reason if the revision changed anything outside the permitted window.

    Compares token sequences rather than characters so that a legitimate grammatical
    agreement (``a`` -> ``an``) next to the span is tolerated, while a reworded clause
    elsewhere in the sentence is not.
    """
    original_tokens = tokenize(original)
    revised_tokens = tokenize(revised)
    first, last = _span_token_range(original_tokens, *span)
    allowed_low, allowed_high = first - margin, last + margin

    matcher = SequenceMatcher(
        a=[t[0] for t in original_tokens], b=[t[0] for t in revised_tokens], autojunk=False
    )
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # An insertion at position i1 sits between tokens i1-1 and i1.
        low, high = (i1, i2 - 1) if i2 > i1 else (i1 - 1, i1)
        if low < allowed_low or high > allowed_high:
            changed = " ".join(t[0] for t in original_tokens[max(0, low): high + 1]) or "(inserted text)"
            return (
                f"over-edit: {tag} outside the flagged span at token {low}"
                f" (allowed {max(0, allowed_low)}-{allowed_high}); touched {changed!r}"
            )
    return None


def parse_response(raw: str) -> dict[str, Any]:
    """Strictly parse a model response. Raises ValueError on anything unexpected."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    missing = {"decision", "revised_sentence", "justification"} - set(payload)
    if missing:
        raise ValueError(f"response is missing {sorted(missing)}")
    if payload["decision"] not in DECISIONS:
        raise ValueError(f"unknown decision {payload['decision']!r}")
    for key in ("revised_sentence", "justification"):
        if not isinstance(payload[key], str):
            raise ValueError(f"{key} must be a string")
    return payload


def validate(hit: Hit, raw: str, *, model: str, prompt_version: str, hash_: str,
             request_id: str | None = None, latency_ms: int = 0,
             cached: bool = False) -> JudgeOutcome:
    """Turn a raw model response into a validated outcome, failing closed."""
    sentence = hit.sentence or hit.paragraph_text

    def escalate(reason: str, raw_decision: str | None = None) -> JudgeOutcome:
        return JudgeOutcome(
            hit=hit, decision="escalate", revised_sentence=sentence,
            justification=f"Automatically escalated: {reason}",
            model=model, prompt_version=prompt_version, prompt_hash=hash_,
            request_id=request_id, latency_ms=latency_ms,
            rejected_reason=reason, raw_decision=raw_decision, cached=cached,
        )

    try:
        payload = parse_response(raw)
    except ValueError as exc:
        return escalate(str(exc))

    decision = payload["decision"]
    revised = payload["revised_sentence"]
    justification = payload["justification"].strip()

    if decision == "change":
        span_start = sentence.find(hit.matched_text)
        if span_start < 0:
            return escalate("flagged text not found in the sentence", decision)

        over_edit = detect_over_edit(
            sentence, revised, (span_start, span_start + len(hit.matched_text))
        )
        if over_edit is not None:
            return escalate(over_edit, decision)

        if hit.approved_text.casefold() not in revised.casefold():
            # Tolerate a re-cased or inflected form of the approved term, but not absence.
            head = hit.approved_text.split()[0].casefold()
            if head not in revised.casefold():
                return escalate(
                    f"decision is 'change' but the approved term "
                    f"{hit.approved_text!r} is absent from the revision",
                    decision,
                )
    else:
        # keep / escalate must not smuggle in an edit.
        if revised.strip() != sentence.strip():
            return escalate(f"decision is {decision!r} but the sentence was modified", decision)
        revised = sentence

    return JudgeOutcome(
        hit=hit, decision=decision, revised_sentence=revised, justification=justification,
        model=model, prompt_version=prompt_version, prompt_hash=hash_,
        request_id=request_id, latency_ms=latency_ms, raw_decision=decision, cached=cached,
    )


# --------------------------------------------------------------------- cache


class ResponseCache:
    """SQLite cache keyed by (sentence, rule id, prompt hash). Makes re-runs free."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS judge_cache (
                key TEXT PRIMARY KEY,
                sentence TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                response TEXT NOT NULL,
                request_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.commit()

    @staticmethod
    def key(sentence: str, rule_id: str, hash_: str, model: str) -> str:
        return hashlib.sha256(f"{sentence}\x00{rule_id}\x00{hash_}\x00{model}".encode()).hexdigest()

    def get(self, key: str) -> tuple[str, str | None] | None:
        row = self._connection.execute(
            "SELECT response, request_id FROM judge_cache WHERE key = ?", (key,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def put(self, key: str, *, sentence: str, rule_id: str, hash_: str, model: str,
            response: str, request_id: str | None) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO judge_cache "
            "(key, sentence, rule_id, prompt_hash, model, response, request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, sentence, rule_id, hash_, model, response, request_id),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


# -------------------------------------------------------------------- judge


class Judge:
    """Runs the judgment step, live or from recorded fixtures.

    ``live=False`` (the default) reads recorded responses, so the whole pipeline and its
    tests run with no API key and no cost. Tests never call the API (CLAUDE.md).
    """

    def __init__(
        self,
        rulebook: Rulebook,
        settings: Settings | None = None,
        *,
        live: bool | None = None,
        cache: ResponseCache | None = None,
        fixtures: dict[str, str] | None = None,
    ) -> None:
        self.rulebook = rulebook
        self.settings = settings or get_settings()
        self.live = self.settings.llm_live if live is None else live
        self.model = self.settings.anthropic_model
        self.cache = cache
        self.fixtures = fixtures if fixtures is not None else load_fixtures(self.settings.fixture_dir)
        self._client: Any = None
        self.stats: dict[str, int] = {"live_calls": 0, "cache_hits": 0, "fixture_hits": 0}

    # -- transport ---------------------------------------------------------

    def _anthropic(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise JudgeError("the anthropic package is required for live judging") from exc
            self._client = anthropic.Anthropic()
        return self._client

    def _call_live(self, system: str, user: str) -> tuple[str, str | None, int]:
        """One API call. Returns (raw JSON text, request id, latency in ms)."""
        client = self._anthropic()
        started = time.perf_counter()
        # No `temperature`: removed from the API for current models. Determinism comes
        # from the constrained schema, low effort, and the response cache.
        response = client.messages.create(
            model=self.model,
            max_tokens=self.settings.llm_max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": self.settings.llm_effort,
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = "".join(block.text for block in response.content if block.type == "text")
        return text, getattr(response, "_request_id", None), latency_ms

    # -- entry points ------------------------------------------------------

    def judge(self, hit: Hit) -> JudgeOutcome:
        """Judge one hit. Never raises on a bad model response; escalates instead."""
        rule = self.rulebook.get(hit.rule_id)
        system, user = build_prompts(hit, rule)
        hash_ = prompt_hash(system)
        sentence = hit.sentence or hit.paragraph_text
        key = ResponseCache.key(sentence, hit.rule_id, hash_, self.model)

        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                raw, request_id = cached
                return validate(hit, raw, model=self.model, prompt_version=PROMPT_VERSION,
                                hash_=hash_, request_id=request_id, cached=True)

        if not self.live:
            raw = self._fixture_for(hit)
            self.stats["fixture_hits"] += 1
            return validate(hit, raw, model=f"{self.model} (fixture)",
                            prompt_version=PROMPT_VERSION, hash_=hash_)

        raw, request_id, latency_ms = self._call_live(system, user)
        self.stats["live_calls"] += 1
        if self.cache is not None:
            self.cache.put(key, sentence=sentence, rule_id=hit.rule_id, hash_=hash_,
                           model=self.model, response=raw, request_id=request_id)
        return validate(hit, raw, model=self.model, prompt_version=PROMPT_VERSION,
                        hash_=hash_, request_id=request_id, latency_ms=latency_ms)

    def judge_all(self, hits: Sequence[Hit]) -> list[JudgeOutcome]:
        """Judge every needs_judgment hit. Hits of other classifications are refused."""
        wrong = [h for h in hits if not h.needs_judgment]
        if wrong:
            raise JudgeError(
                f"the LLM may only see needs_judgment hits; got {len(wrong)} others "
                "(constraint 3)"
            )
        return [self.judge(hit) for hit in hits]

    # -- fixtures ----------------------------------------------------------

    def _fixture_for(self, hit: Hit) -> str:
        """A recorded response for this hit, or a deterministic stand-in.

        The stand-in mirrors the rulebook's own guidance so a dry run produces a
        realistic mix of change / keep decisions without inventing outcomes that the
        rulebook would not support.
        """
        for key in (
            f"{hit.rule_id}|{hit.matched_text.casefold()}|{(hit.sentence or '').casefold()}",
            f"{hit.rule_id}|{hit.matched_text.casefold()}",
            hit.rule_id,
        ):
            if key in self.fixtures:
                return self.fixtures[key]
        return default_fixture_response(hit, self.rulebook.get(hit.rule_id))


def default_fixture_response(hit: Hit, rule: Rule) -> str:
    """A deterministic stand-in response used when no fixture is recorded.

    Patient-facing plain language keeps the deprecated term; everything else takes the
    approved one. Deterministic by construction, so dry runs are reproducible.
    """
    sentence = hit.sentence or hit.paragraph_text
    patient_facing = any(
        marker in (hit.paragraph_text + " " + hit.location.describe()).casefold()
        for marker in ("you may experience", "your care team", "for the patient",
                       "tell your", "talk to your", "what you may")
    )
    if patient_facing and rule.context_required:
        return json.dumps({
            "decision": "keep",
            "revised_sentence": sentence,
            "justification": (
                "The surrounding text addresses the patient directly, so the "
                "plain-language term is the readability-tested wording."
            ),
        })

    start = sentence.find(hit.matched_text)
    if start < 0:
        return json.dumps({
            "decision": "escalate",
            "revised_sentence": sentence,
            "justification": "The flagged span could not be located in the sentence.",
        })
    revised = sentence[:start] + hit.approved_text + sentence[start + len(hit.matched_text):]
    return json.dumps({
        "decision": "change",
        "revised_sentence": revised,
        "justification": (
            f"The context is professional or regulatory, so {rule.approved!r} is the "
            "required term here."
        ),
    })


def load_fixtures(directory: Path) -> dict[str, str]:
    """Load recorded responses from a fixture directory.

    Each ``*.json`` file maps a fixture key to a response object or raw string.
    """
    directory = Path(directory)
    fixtures: dict[str, str] = {}
    if not directory.exists():
        return fixtures
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text())
        for key, value in payload.items():
            fixtures[key] = value if isinstance(value, str) else json.dumps(value)
    return fixtures
