#!/usr/bin/env python3
"""Check the final documents with an unrelated tool, and compare verdicts.

    python scripts/second_opinion.py [--final-dir data/out/final]

TermGuard verifying its own output with its own scanner is a closed loop: if the scanner
has a blind spot, the gate inherits it. This runs a second, independent check - Vale, with
a style generated from the same rulebook - over the same final documents, and reports
whether the two tools agree.

Two unrelated implementations agreeing on zero violations is a much stronger claim than
one tool agreeing with itself, and it is the kind of evidence a validation reviewer asks
for.

Requires pandoc and Vale, neither of which TermGuard depends on:
    brew install pandoc vale          # macOS
    apt-get install pandoc            # Linux; Vale from github.com/errata-ai/vale
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from termguard.config import get_settings  # noqa: E402
from termguard.rulebook import CaseKind, MatchKind, Rulebook, load_rulebook  # noqa: E402


def require(tool: str, hint: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise SystemExit(f"{tool} is not installed.\n    {hint}")
    return path


def write_vale_style(rulebook: Rulebook, styles_dir: Path) -> list[str]:
    """One Vale substitution rule per terminology rule.

    Rules the rulebook marks context_required are deliberately skipped: Vale has no notion
    of "a human must decide", so including them would manufacture disagreement where the
    two tools are simply answering different questions.
    """
    target = styles_dir / "TermGuard"
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    for rule in rulebook:
        if rule.context_required:
            continue
        swaps = {
            (term if rule.match is MatchKind.REGEX else re.escape(term)): rule.approved
            for term in rule.deprecated
        }
        body = [
            f"# {rule.id}: generated from data/rulebook.yaml - do not edit by hand",
            "extends: substitution",
            f"message: \"{rule.id}: use '%s' instead of '%s'\"",
            "level: error",
            f"ignorecase: {str(rule.case is not CaseKind.EXACT).lower()}",
            "swap:",
        ]
        body += [f"  {pattern}: {replacement}" for pattern, replacement in swaps.items()]
        (target / f"{rule.id}.yml").write_text("\n".join(body) + "\n")
        written.append(rule.id)

    return written


def run_vale(vale: str, config: Path, documents: list[Path]) -> dict[str, Any]:
    result = subprocess.run(
        [vale, "--config", str(config), "--output=JSON", *[str(p) for p in documents]],
        capture_output=True, text=True,
    )
    if not result.stdout.strip():
        if result.returncode not in (0, 1):
            raise SystemExit(f"vale failed: {result.stderr.strip()}")
        return {}
    return json.loads(result.stdout)


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--final-dir", type=Path, default=settings.final_dir)
    parser.add_argument("--rulebook", type=Path, default=settings.rulebook_path)
    parser.add_argument("--work-dir", type=Path, default=settings.out_dir / "second-opinion")
    args = parser.parse_args()

    pandoc = require("pandoc", "brew install pandoc  (or apt-get install pandoc)")
    vale = require("vale", "brew install vale  (or see github.com/errata-ai/vale)")

    finals = sorted(args.final_dir.glob("*.docx"))
    if not finals:
        raise SystemExit(
            f"no final documents in {args.final_dir}\n"
            "    run `make demo` first, or pass --final-dir"
        )

    rulebook = load_rulebook(args.rulebook)
    work = args.work_dir
    markdown_dir = work / "markdown"
    markdown_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nsecond opinion on {len(finals)} final document(s)")
    print(f"  rulebook {rulebook.hash}, converting with pandoc\n")

    converted: list[Path] = []
    for path in finals:
        destination = markdown_dir / f"{path.stem}.md"
        subprocess.run([pandoc, str(path), "-t", "markdown", "-o", str(destination)],
                       check=True, capture_output=True)
        converted.append(destination)

    rule_ids = write_vale_style(rulebook, work / "styles")
    (work / ".vale.ini").write_text(
        f"StylesPath = styles\nMinAlertLevel = error\n\n[*.md]\nBasedOnStyles = TermGuard\n"
    )
    print(f"  generated {len(rule_ids)} Vale rules "
          f"({len(rulebook) - len(rule_ids)} context_required rules skipped -\n"
          f"  Vale cannot express 'a human must decide', so asking it to would\n"
          f"  manufacture disagreement rather than reveal any)\n")

    alerts = run_vale(vale, work / ".vale.ini", converted)
    flagged = {name: items for name, items in alerts.items() if items}

    print("  independent check")
    if not flagged:
        print(f"    Vale found 0 violations across {len(converted)} documents.")
        print("    Both tools agree the corpus is clean.")
        return 0

    total = sum(len(items) for items in flagged.values())
    print(f"    Vale found {total} violation(s) TermGuard's own gate did not:\n")
    for name, items in flagged.items():
        for alert in items[:10]:
            print(f"      {Path(name).name}:{alert['Line']}  {alert['Message']}")
    print("\n    The two tools disagree. Either the scanner has a blind spot, or a")
    print("    ratified exception is being reported by a tool that has no concept of one.")
    print("    Check data/out/verification.json for ratified exceptions before concluding")
    print("    the scanner is wrong.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
