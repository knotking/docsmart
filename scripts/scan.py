#!/usr/bin/env python3
"""Scan a corpus and print the summary table. Read-only: writes nothing, calls no LLM.

    python scripts/scan.py [corpus_dir] [--json out.json] [--report-only]

``--report-only`` adds a sizing estimate - what a prospect wants to see before committing
their real documents to anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from termguard.config import get_settings  # noqa: E402
from termguard.rulebook import load_rulebook  # noqa: E402
from termguard.scanner import build_report  # noqa: E402

REVIEW_RATE_PER_HOUR = 40


def bar(value: int, total: int, width: int = 28) -> str:
    filled = 0 if total == 0 else round(width * value / total)
    return "#" * filled + "." * (width - filled)


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", nargs="?", default=str(settings.corpus_dir))
    parser.add_argument("--rulebook", default=str(settings.rulebook_path))
    parser.add_argument("--json", dest="json_out", help="also write the full report as JSON")
    parser.add_argument("--report-only", action="store_true",
                        help="add a reviewer-effort sizing estimate")
    args = parser.parse_args()

    rulebook = load_rulebook(args.rulebook)
    report = build_report(args.corpus, rulebook)

    print(f"\nTermGuard scan - {args.corpus}")
    print(f"rulebook {Path(args.rulebook).name} (hash {rulebook.hash}), "
          f"{len(rulebook)} rules, {report.files_scanned} files\n")

    print(f"  total hits         {report.total}")
    print(f"    unambiguous      {report.unambiguous:4}  {bar(report.unambiguous, report.total)}")
    print(f"    needs judgment   {report.needs_judgment:4}  {bar(report.needs_judgment, report.total)}")

    print("\n  by document part")
    for part, count in sorted(report.by_part().items(), key=lambda kv: -kv[1]):
        print(f"    {part:12} {count:4}  {bar(count, report.total)}")

    print("\n  by rule")
    for rule_id, count in report.by_rule().items():
        rule = rulebook.get(rule_id)
        flag = " (context)" if rule.context_required else ""
        print(f"    {rule_id} {count:4}  {rule.deprecated[0][:22]:24} -> {rule.approved[:24]}{flag}")

    print("\n  by file")
    for name, count in sorted(report.by_file().items()):
        print(f"    {name:20} {count:4}")
    clean = report.files_scanned - len(report.by_file())
    if clean:
        print(f"    ({clean} file(s) with no hits)")

    hidden = sum(v for k, v in report.by_part().items() if k != "body")
    print(f"\n  {hidden} hits sit outside the document body "
          f"(headers, footers, footnotes, text boxes) - the ones a manual pass misses.")

    if args.report_only:
        hours = report.needs_judgment / REVIEW_RATE_PER_HOUR
        print("\n  sizing estimate")
        print(f"    deterministic fixes (no reviewer time)   {report.unambiguous}")
        print(f"    decisions needing a reviewer             {report.needs_judgment}")
        print(f"    projected reviewer effort                {hours:.1f} hours "
              f"at {REVIEW_RATE_PER_HOUR} decisions/hour")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report.to_dict(), indent=2))
        print(f"\n  wrote {args.json_out}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
