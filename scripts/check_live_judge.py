#!/usr/bin/env python3
"""Exercise the judgment prompt against the real API, and grade what comes back.

    export ANTHROPIC_API_KEY=sk-...
    python scripts/check_live_judge.py                 # two documents, ~15 calls
    python scripts/check_live_judge.py --all           # the whole corpus
    python scripts/check_live_judge.py --rule R-002    # one rule only

Why this exists: the whole pipeline runs from recorded fixtures by default, which is
right for tests and for a demo with no key. But it means the *prompt* can be wrong and
everything still passes. The containment around the model is well tested - malformed
JSON, over-edits, missing terms are all rejected - and none of that says the model makes
good judgments, because until this script runs it has never been asked.

The graded case is R-002, `side effect`. The rulebook says it must become
`adverse event` in clinical and risk documents, and must be left alone in patient-facing
plain language. Same term, opposite answers, decided only by context. If the model cannot
make that distinction reliably, the demo's central claim does not hold and you want to
know before a prospect does, not after.

Nothing is written: no documents, no database rows, no versions. This reads the corpus,
calls the model, and prints a report.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from termguard.config import get_settings  # noqa: E402
from termguard.judge import Judge, JudgeOutcome, ResponseCache  # noqa: E402
from termguard.rulebook import load_rulebook  # noqa: E402
from termguard.scanner import scan_document  # noqa: E402

# Documents whose R-002 answer we know, and why. The scanner cannot tell these apart -
# that is the entire point of routing them to judgment.
EXPECTED = {
    "IFU": ("keep", "patient-facing plain language: 'Tell your care team about any "
                    "side effect you notice'"),
    "LBL": ("keep", "labeling draft, 'For the patient' section"),
    "CER": ("change", "clinical evaluation report: the regulatory term is required"),
    "RMS": ("change", "risk management summary: the regulatory term is required"),
}

DEFAULT_FILES = ("IFU-001.docx", "CER-001.docx")


def expectation(file_name: str) -> tuple[str, str] | None:
    return EXPECTED.get(file_name.split("-")[0].upper())


def grade(outcome: JudgeOutcome) -> tuple[str, str]:
    """Compare one live answer against what the rulebook implies for that document type."""
    expected = expectation(outcome.hit.file)
    if outcome.hit.rule_id != "R-002" or expected is None:
        return "ungraded", ""
    wanted, why = expected
    if outcome.decision == wanted:
        return "correct", why
    if outcome.decision == "escalate":
        return "escalated", outcome.rejected_reason or outcome.justification
    return "wrong", f"said {outcome.decision}, expected {wanted} - {why}"


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=settings.corpus_dir)
    parser.add_argument("--all", action="store_true", help="every document, not just two")
    parser.add_argument("--rule", help="only judge hits for this rule id")
    parser.add_argument("--limit", type=int, default=0, help="stop after N calls")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore the response cache and call the API every time")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "No credential found.\n"
            "    export ANTHROPIC_API_KEY=sk-...\n"
            "This script only makes live calls; there is nothing to run without one.",
            file=sys.stderr,
        )
        return 2

    rulebook = load_rulebook(settings.rulebook_path)
    paths = sorted(args.corpus.glob("*.docx"))
    if not args.all:
        chosen = [p for p in paths if p.name in DEFAULT_FILES]
        paths = chosen or paths[:2]
    if not paths:
        print(f"no .docx files in {args.corpus}", file=sys.stderr)
        return 1

    hits = [
        hit
        for path in paths
        for hit in scan_document(path, rulebook)
        if hit.needs_judgment and (not args.rule or hit.rule_id == args.rule)
    ]
    if args.limit:
        hits = hits[: args.limit]
    if not hits:
        print("no needs_judgment hits to send", file=sys.stderr)
        return 1

    cache = None if args.no_cache else ResponseCache(settings.out_dir / "judge-cache.db")
    judge = Judge(rulebook, settings, live=True, cache=cache)

    print(f"\nmodel      {judge.model}")
    print(f"documents  {', '.join(p.name for p in paths)}")
    print(f"sending    {len(hits)} sentence(s) - one rule and one sentence per call\n")

    outcomes: list[JudgeOutcome] = []
    for index, hit in enumerate(hits, start=1):
        outcome = judge.judge(hit)
        outcomes.append(outcome)
        verdict, detail = grade(outcome)
        mark = {"correct": "ok ", "wrong": "XX ", "escalated": "-> ", "ungraded": "   "}[verdict]
        source = "cached" if outcome.cached else f"{outcome.latency_ms}ms"
        print(f"  {mark}[{index:>3}/{len(hits)}] {hit.rule_id} {hit.file:14} "
              f"{outcome.decision:9} {source:>8}  {hit.matched_text!r}")
        if verdict in ("wrong", "escalated"):
            print(f"        {detail}")
        if outcome.decision == "change" and not outcome.was_rejected:
            print(f"        -> {outcome.revised_sentence[:96]}")

    # ---------------------------------------------------------------- report
    graded = [(o, *grade(o)) for o in outcomes]
    scored = [g for g in graded if g[1] in ("correct", "wrong")]
    correct = sum(1 for g in scored if g[1] == "correct")
    decisions = Counter(o.decision for o in outcomes)
    rejected = [o for o in outcomes if o.was_rejected]
    latencies = [o.latency_ms for o in outcomes if not o.cached and o.latency_ms]

    print(f"\n{'=' * 68}")
    print(f"  decisions          {dict(decisions)}")
    print(f"  live calls         {judge.stats['live_calls']}  "
          f"(cache hits {judge.stats['cache_hits']})")
    if latencies:
        print(f"  latency            median {statistics.median(latencies):.0f}ms  "
              f"max {max(latencies)}ms")
    print(f"  request ids        {sum(1 for o in outcomes if o.request_id)}/{len(outcomes)}")
    print(f"  prompt version     {outcomes[0].prompt_version} "
          f"(hash {outcomes[0].prompt_hash})")

    print(f"\n  rejected by validation: {len(rejected)}")
    for outcome in rejected[:8]:
        print(f"      {outcome.hit.rule_id} {outcome.hit.file}: {outcome.rejected_reason}")
    if rejected:
        print("      (these were refused by code and routed to a human - the containment"
              " working)")

    if scored:
        print(f"\n  R-002 context discrimination: {correct}/{len(scored)} correct")
        for outcome, verdict, detail in scored:
            if verdict == "wrong":
                print(f"      WRONG  {outcome.hit.file}: {detail}")
                print(f"             sentence: {outcome.hit.sentence[:88]}")
        if correct == len(scored):
            print("      The model kept patient-facing plain language and applied the")
            print("      regulatory term in clinical context, every time.")
    else:
        print("\n  No gradable R-002 hits in this selection "
              "(try --rule R-002, or --all).")

    print(f"{'=' * 68}\n")

    if cache is not None:
        cache.close()

    # Wrong answers are the thing worth failing on. Escalations are the design working.
    wrong = len(scored) - correct
    if wrong:
        print(f"{wrong} answer(s) contradicted the rulebook. Read the context notes for")
        print("those rules - the prompt carries them verbatim, so a wrong answer usually")
        print("means the note is ambiguous rather than that the model misread it.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
