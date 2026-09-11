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

from termguard import documents, review, teams, verify, workflow  # noqa: E402
from termguard.models import (  # noqa: E402
    DecisionKind, ParticipantKind, Role, SignOffDecision, TeamRole,
)
from termguard.policy import load_policy  # noqa: E402
from termguard.rulebook import load_rulebook  # noqa: E402
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
    parser.add_argument("--solo", action="store_true",
                        help="one reviewer accepting everything, instead of the "
                             "multi-participant workflow")
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

    if args.solo:
        heading("2. Review: one reviewer accepting everything (dry-run only)")
        with session_scope(settings) as session:
            accepted = review.auto_accept_all(session, run_id, reviewer=args.reviewer)
        print(f"  {accepted} changes accepted by {args.reviewer}")
    else:
        _run_workflow(settings, run_id, args.reviewer)

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

    if not args.solo and passed:
        heading("4. Sign-off (maker-checker)")
        with session_scope(settings) as session:
            policy = load_policy(REPO_ROOT / "data" / "policy.yaml")
            readiness = workflow.signoff_readiness(session, run_id)
            print(f"  ready: {readiness['ready']}")
            print(f"  eligible approvers: {', '.join(readiness['eligible_approvers']) or 'none'}")

            reviewer = workflow.get_participant(session, "alice@meridian")
            ok, why = workflow.separation_of_duties(session, run_id, reviewer)
            print(f"\n  can alice (who reviewed) approve? {ok}")
            for reason in why:
                print(f"    - {reason}")

            approver = workflow.get_participant(session, "dana@meridian")
            record = workflow.sign_off(
                session, run_id, approver, SignOffDecision.APPROVED,
                note="Terminology review complete; verification passed.", policy=policy,
            )
            print(f"\n  {approver.name} approved run {run_id}")
            print(f"    covered:       {record.covered}")
            print(f"    rulebook hash: {record.rulebook_hash}")
            print(f"    policy hash:   {record.policy_hash}")

    heading("5. Document lifecycle" if not args.solo else "4. Document lifecycle")
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


def _run_workflow(settings, run_id: int, approver_email: str) -> None:
    """Two reviewers and an agent clear the queue, with claims and policy in force."""
    heading("2. Review: five teams, four people and an agent")
    policy = load_policy(REPO_ROOT / "data" / "policy.yaml")
    rulebook = load_rulebook(settings.rulebook_path)

    with session_scope(settings) as session:
        # --- org and teams, derived from who owns each rule -----------------
        org = teams.ensure_org(session, "Meridian Medical", "meridian")
        made = teams.teams_from_rulebook(session, rulebook, org)
        print(f"  org {org.name}: {len(made)} teams, one per rule owner")
        for slug, team in sorted(made.items()):
            print(f"    {slug:22} {team.description}")
        missing = teams.unrouted_owners(session, rulebook)
        if missing:
            print(f"    owners with no team: {missing}")

        # --- people ----------------------------------------------------------
        alice = workflow.ensure_participant(session, "alice@meridian", roles=[Role.REVIEWER])
        bob = workflow.ensure_participant(session, "bob@meridian", roles=[Role.REVIEWER])
        carol = workflow.ensure_participant(session, "carol@meridian", roles=[Role.REVIEWER])
        dana = workflow.ensure_participant(session, "dana@meridian", roles=[Role.APPROVER])
        agent = workflow.ensure_participant(
            session, "termguard-agent", kind=ParticipantKind.AGENT, roles=[Role.REVIEWER],
            model=settings.anthropic_model, policy_hash=policy.hash,
        )
        teams.add_member(session, made["clinical-affairs"], alice)
        teams.add_member(session, made["clinical-affairs"], bob, role=TeamRole.LEAD)
        teams.add_member(session, made["regulatory-affairs"], carol, role=TeamRole.LEAD)
        for slug in ("technical-writing", "quality-assurance", "systems-engineering"):
            teams.add_member(session, made[slug], alice)
            teams.add_member(session, made[slug], bob)
        print(f"\n  alice, bob (clinical + 3 others; bob leads clinical), "
              f"carol (leads regulatory), dana (approver), {agent.name}")

        # --- the agent takes what policy allows ------------------------------
        outcome = workflow.agent_dispose(session, run_id, agent, policy)
        print(f"\n  agent decided {outcome.decided} "
              f"({', '.join(f'{k}:{v}' for k, v in outcome.by_clause.items())}), "
              f"left {outcome.left_to_humans} to humans")
        if outcome.needs_confirmation:
            print(f"    {outcome.needs_confirmation} of those need a human to confirm")

        # --- the rest routes to the team that owns the rule ------------------
        routed = workflow.route_by_rule_owner(session, run_id, rulebook)
        print(f"\n  routed to the owning team: "
              + ", ".join(f"{slug.split('-')[0]} {n}" for slug, n in sorted(routed["routed"].items())))
        if routed["unroutable"]:
            print(f"    {routed['unroutable']} could not be routed")

        # --- one change takes the scenic route --------------------------------
        queue = workflow.queue_for(session, run_id, alice)
        if queue:
            travelled = queue[0]
            workflow.escalate(session, run_id, travelled, actor=alice,
                              reason="cannot tell if this section is patient-facing")
            workflow.reassign(session, run_id, travelled, actor=bob, to_participant=carol,
                              reason="regulatory owns the quoted-CFR reading")
            print(f"\n  change {travelled}: alice escalated it to bob (clinical lead),")
            print(f"    who handed it to carol in regulatory - state now "
                  f"{workflow.change_state(session, travelled)}")

        # --- alice goes on leave ------------------------------------------------
        teams.delegate(session, alice, bob, reason="annual leave", created_by="dana@meridian")
        print(f"\n  alice is away; her work is covered by "
              f"{teams.effective_owner(session, alice).name}")

        # --- everyone works their queue ------------------------------------------
        decided = 0
        for change in list(review.pending_changes(session, run_id)):
            owner = (
                workflow.current_assignee(session, change.id)
                or _pool_member(session, change.id, [bob, carol, alice])
            )
            owner = teams.effective_owner(session, owner)
            workflow.claim(session, change.id, owner)
            review.record_decision(session, change.id, DecisionKind.ACCEPTED, owner.name,
                                   participant=owner)
            workflow.release(session, change.id, owner)
            decided += 1
        print(f"  reviewers decided {decided}")

        confirmed = workflow.confirm_agent_batch(session, run_id, bob)
        if confirmed:
            print(f"  bob confirmed {confirmed} agent decision(s) that required it")


def _pool_member(session, change_id: int, candidates):
    """Whoever on the owning team picks a pooled change up first."""
    from termguard.models import Team

    assignment = workflow.current_assignment(session, change_id)
    if assignment is None or assignment.team_id is None:
        return candidates[0]
    team = session.get(Team, assignment.team_id)
    members = teams.members(session, team) if team else []
    human = [p for p in members if not p.is_agent]
    return human[0] if human else candidates[0]


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
