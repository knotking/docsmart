#!/usr/bin/env python3
"""Run the whole pipeline end to end and stop on the verification gate.

    python scripts/demo.py [--reviewer NAME] [--reset] [--inject-stray-edit]

``--inject-stray-edit`` hand-edits one final document after review, to demonstrate that
the gate catches an edit nobody approved.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from termguard import documents, review, verify  # noqa: E402
from termguard.config import get_settings  # noqa: E402
from termguard.db import init_db, session_scope  # noqa: E402
from termguard.pipeline import run_pipeline  # noqa: E402
from termguard.storage import get_store  # noqa: E402


def heading(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewer", default="demo-reviewer@meridian")
    parser.add_argument("--reset", action="store_true", help="start from an empty database")
    parser.add_argument("--inject-stray-edit", action="store_true",
                        help="prove the gate catches an unapproved edit")
    args = parser.parse_args()

    settings = get_settings()
    if args.reset:
        for path in (Path(settings.db_url.replace("sqlite:///", "")), settings.out_dir,
                     settings.blob_root):
            if path.exists():
                shutil.rmtree(path) if path.is_dir() else path.unlink()

    init_db(settings)
    store = get_store(settings)
    started = time.time()

    heading("1. Pipeline: ingest -> scan -> redline -> judge")
    with session_scope(settings) as session:
        run, outcomes = run_pipeline(
            session, settings=settings, store=store, dry_run=True, actor=args.reviewer,
            note="make demo",
        )
        run_id = run.id
        stats = dict(run.stats)

    print(f"  files            {stats['files']}")
    print(f"  hits             {stats['hits']}")
    print(f"  deterministic    {stats['deterministic']}")
    print(f"  AI-proposed      {stats['ai']}")
    print(f"  AI kept as-is    {stats['ai_kept']}")
    print(f"  AI escalated     {stats['ai_escalated']}")
    print(f"  skipped          {stats['skipped']}")

    heading("2. Review: auto-accepting every change (dry-run only)")
    with session_scope(settings) as session:
        accepted = review.auto_accept_all(session, run_id, reviewer=args.reviewer)
    print(f"  {accepted} changes accepted by {args.reviewer}")

    if args.inject_stray_edit:
        heading("2b. Injecting an unapproved edit into the reviewed document")
        with session_scope(settings) as session:
            from sqlmodel import select

            from termguard.models import Document, Stage

            target = session.exec(
                select(Document).where(Document.name == "SOP-001.docx")
            ).one()
            redlined = documents.latest_at_stage(session, target.id, Stage.REDLINED)
            tampered = _inject(documents.content(store, redlined))
            documents.add_version(
                session, store, target, tampered, Stage.REDLINED,
                actor="someone with the file open", parent=redlined, run_id=run_id,
                note="hand-edited outside the tool - this must not survive the gate",
            )
        print("  SOP-001.docx was hand-edited after review, outside TermGuard")
        print("  (the gate must now FAIL with an unexplained edit)")

    heading("3. Verification gate")
    with session_scope(settings) as session:
        report = verify.verify_run(session, run_id, settings=settings, store=store,
                                   actor=args.reviewer)
        totals = report.totals
        passed = report.passed

    print(f"  files verified          {totals['files']}")
    print(f"  clean                   {totals['passed']}")
    print(f"  failing                 {totals['failed']}")
    print(f"  remaining violations    {totals['remaining_hits']}")
    print(f"  undecided changes       {totals['undecided']}")
    print(f"  unexplained edits       {totals['unexplained_edits']}")
    print(f"\n  VERDICT: {'PASS' if passed else 'FAIL'}")

    if not passed:
        for item in report.files:
            if not item.passed:
                print(f"    {item.name}: {'; '.join(item.failure_reasons)}")

    heading("4. Document lifecycle")
    with session_scope(settings) as session:
        from sqlmodel import select
        from termguard.models import Document

        sample = session.exec(select(Document).where(Document.name == "IFU-001.docx")).one()
        for version in documents.history(session, sample.id):
            print(f"  v{version.version_no} {version.stage.value:9} "
                  f"sha={version.content_sha256[:12]} {version.size_bytes:>7}b  {version.actor[:40]}")
        integrity = documents.verify_integrity(session, store)
        print(f"\n  integrity: {integrity['checked']} versions checked, "
              f"{'all intact' if integrity['ok'] else str(len(integrity['failures'])) + ' FAILURES'}")

    print(f"\n  outputs: {settings.redlined_dir.relative_to(REPO_ROOT)}/ "
          f"and {settings.final_dir.relative_to(REPO_ROOT)}/")
    print(f"  report:  {(settings.out_dir / 'verification.md').relative_to(REPO_ROOT)}")
    print(f"  elapsed: {time.time() - started:.1f}s\n")
    return 0 if (passed != args.inject_stray_edit) else 1


def _inject(data: bytes) -> bytes:
    """Hand-edit a document the way someone with it open in Word would.

    Not a tracked change and not anything a decision covers - exactly the edit the
    gate exists to catch.
    """
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        parts = {n: zf.read(n) for n in zf.namelist()}
    body = parts["word/document.xml"].decode()
    parts["word/document.xml"] = body.replace(
        "</w:t>", " (revised per meeting)</w:t>", 1
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in parts.items():
            out.writestr(name, blob)
    return buffer.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
