"""Layer D, part 2: the verification gate.

"Consistent" has to be provable, so the pipeline is not done until a re-scan of the
reviewed output reports zero hits (constraint 6). Three things are checked, and any one of
them failing fails the run:

1. **Every change is decided.** An undecided or escalated change blocks verification and
   is listed by name. Silence is not consent.
2. **The final text is clean.** The as-accepted document is re-scanned with the *same*
   rulebook hash. Zero hits, or the gate fails.
3. **Nothing else was touched.** Every paragraph that differs between the original and the
   final document must be explained by an accepted or edited change. A difference that no
   decision accounts for fails as an ``unexplained edit`` - which is what proves the tool
   changed only what it said it changed.

The as-accepted document is built by resolving revisions in place (accept keeps the
insertion, reject restores the deletion, edit substitutes the reviewer's wording), then
stripping review comments, and is stored as a new ``final`` document version.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from lxml import etree
from sqlmodel import Session, select

from termguard import audit, documents, ooxml
from termguard.config import Settings, get_settings
from termguard.models import (
    ActorKind,
    Change,
    ChangeStatus,
    Decision,
    DecisionKind,
    Document,
    DocumentVersion,
    Run,
    RunStatus,
    Stage,
)
from termguard.rulebook import Rulebook, load_rulebook
from termguard.scanner import Hit, scan_bytes
from termguard.storage import ObjectStore, get_store
from termguard.walker import walk_bytes

RESOLVABLE_PARTS = ("word/document.xml",)
_PLACEHOLDER = "{}"


@dataclass
class FileVerification:
    """The verdict for one document."""

    name: str
    passed: bool = True
    hits: list[dict[str, Any]] = field(default_factory=list)
    adjudicated: list[dict[str, Any]] = field(default_factory=list)
    undecided: list[dict[str, Any]] = field(default_factory=list)
    disputed: list[dict[str, Any]] = field(default_factory=list)
    unexplained: list[dict[str, Any]] = field(default_factory=list)
    accepted: int = 0
    kept: int = 0
    rejected: int = 0
    edited: int = 0
    deterministic: int = 0
    ai: int = 0
    final_version_id: int | None = None
    final_sha256: str | None = None
    diff: str = ""

    @property
    def failure_reasons(self) -> list[str]:
        reasons = []
        if self.undecided:
            reasons.append(f"{len(self.undecided)} undecided change(s)")
        if self.disputed:
            reasons.append(f"{len(self.disputed)} disputed keep(s) needing a fix")
        if self.hits:
            reasons.append(f"{len(self.hits)} unadjudicated violation(s)")
        if self.unexplained:
            reasons.append(f"{len(self.unexplained)} unexplained edit(s)")
        return reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.name,
            "passed": self.passed,
            "reasons": self.failure_reasons,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "edited": self.edited,
            "kept": self.kept,
            "deterministic": self.deterministic,
            "ai": self.ai,
            "remaining_hits": self.hits,
            "adjudicated_exceptions": self.adjudicated,
            "undecided": self.undecided,
            "disputed": self.disputed,
            "unexplained_edits": self.unexplained,
            "final_version_id": self.final_version_id,
            "final_sha256": self.final_sha256,
        }


@dataclass
class VerificationReport:
    """The run-level verdict. This is the artifact a QA reader is handed."""

    run_id: int
    rulebook_hash: str
    corpus_hash: str
    verified_at: str
    files: list[FileVerification] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.files) and all(f.passed for f in self.files)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "files": len(self.files),
            "passed": sum(1 for f in self.files if f.passed),
            "failed": sum(1 for f in self.files if not f.passed),
            "accepted": sum(f.accepted for f in self.files),
            "rejected": sum(f.rejected for f in self.files),
            "edited": sum(f.edited for f in self.files),
            "kept": sum(f.kept for f in self.files),
            "deterministic": sum(f.deterministic for f in self.files),
            "ai": sum(f.ai for f in self.files),
            "remaining_hits": sum(len(f.hits) for f in self.files),
            "adjudicated_exceptions": sum(len(f.adjudicated) for f in self.files),
            "undecided": sum(len(f.undecided) for f in self.files),
            "disputed": sum(len(f.disputed) for f in self.files),
            "unexplained_edits": sum(len(f.unexplained) for f in self.files),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "rulebook_hash": self.rulebook_hash,
            "corpus_hash": self.corpus_hash,
            "verified_at": self.verified_at,
            "passed": self.passed,
            "totals": self.totals,
            "reviewers": self.reviewers,
            "files": [f.to_dict() for f in self.files],
        }


# ------------------------------------------------------------- decision lookup


def latest_decisions(session: Session, run_id: int) -> dict[int, Decision]:
    """The newest decision per change. Superseded decisions remain as history."""
    rows = session.exec(
        select(Decision).where(Decision.run_id == run_id).order_by(Decision.id)
    ).all()
    latest: dict[int, Decision] = {}
    for row in rows:
        latest[row.change_id] = row  # later rows win
    return latest


# --------------------------------------------------------------- as-accepted


def apply_decisions(
    data: bytes, resolutions: Sequence[tuple[Sequence[int], DecisionKind, str | None]]
) -> bytes:
    """Resolve revisions into a clean document.

    ``resolutions`` is a sequence of ``(revision_ids, decision, final_text)``. Every part
    of the package is resolved, not just the body, because revisions exist in headers,
    footers and footnotes too.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        parts = {name: zf.read(name) for name in zf.namelist()}

    trees: dict[str, etree._Element] = {}
    for name, blob in parts.items():
        if not name.endswith(".xml") or name == "[Content_Types].xml":
            continue
        try:
            trees[name] = etree.fromstring(blob)
        except etree.XMLSyntaxError:  # pragma: no cover
            continue

    for revision_ids, decision, final_text in resolutions:
        accept = decision in (DecisionKind.ACCEPTED, DecisionKind.EDITED)
        replacement = final_text if decision is DecisionKind.EDITED else None
        for root in trees.values():
            for revision_id in revision_ids:
                ooxml.resolve_revision(
                    root, revision_id, accept=accept, replacement_text=replacement
                )

    for root in trees.values():
        ooxml.strip_comment_markers(root)

    # Review comments do not belong in an approved document.
    for name in list(parts):
        if name == "word/comments.xml":
            parts.pop(name)
            trees.pop(name, None)

    for name, root in trees.items():
        parts[name] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in parts.items():
            out.writestr(name, blob)
    return buffer.getvalue()


# ------------------------------------------------------------ diff explanation


def paragraph_texts(data: bytes, name: str) -> dict[tuple[str, int], str]:
    """Every paragraph keyed by (part, index) - the unit the diff check works in."""
    return {
        (p.location.part, p.location.paragraph_index): p.text
        for p in walk_bytes(data, name)
    }


def explain_difference(
    original: str, final: str, expected: Sequence[tuple[str, str]]
) -> bool:
    """True if ``final`` differs from ``original`` only by the expected substitutions.

    Each expected ``(was, now)`` pair is replaced with the same unique placeholder on both
    sides. If the two strings then match, every difference is accounted for by a decision;
    anything left over is an edit nobody approved.
    """
    left, right = original, final
    for index, (was, now) in enumerate(expected):
        token = _PLACEHOLDER.format(index)
        if was and was in left:
            left = left.replace(was, token, 1)
        if now and now in right:
            right = right.replace(now, token, 1)
    return left == right


# ------------------------------------------------------------------- verify


def verify_run(
    session: Session,
    run_id: int,
    *,
    settings: Settings | None = None,
    store: ObjectStore | None = None,
    rulebook: Rulebook | None = None,
    actor: str = "system",
    write_outputs: bool = True,
) -> VerificationReport:
    """Build the as-accepted corpus, re-scan it, and return the verdict."""
    settings = settings or get_settings()
    store = store or get_store(settings)
    rulebook = rulebook or load_rulebook(settings.rulebook_path)

    run = session.get(Run, run_id)
    if run is None:
        raise ValueError(f"no run {run_id}")
    if run.rulebook_hash and run.rulebook_hash != rulebook.hash:
        raise ValueError(
            f"rulebook has changed since run {run_id} "
            f"({run.rulebook_hash} -> {rulebook.hash}); re-run before verifying"
        )

    from termguard.models import utcnow

    report = VerificationReport(
        run_id=run_id,
        rulebook_hash=rulebook.hash,
        corpus_hash=run.corpus_hash,
        verified_at=utcnow().isoformat(),
    )

    decisions = latest_decisions(session, run_id)
    reviewers = sorted({d.reviewer for d in decisions.values() if d.reviewer})
    report.reviewers = reviewers

    documents_in_run = session.exec(
        select(Document)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)  # type: ignore[arg-type]
        .where(DocumentVersion.run_id == run_id)
        .distinct()
        .order_by(Document.name)
    ).all()

    if write_outputs:
        settings.final_dir.mkdir(parents=True, exist_ok=True)

    for document in documents_in_run:
        result = _verify_document(
            session, store, run, document, rulebook, settings, decisions,
            actor=actor, write_outputs=write_outputs,
        )
        if result is not None:
            report.files.append(result)

    run.status = RunStatus.VERIFIED if report.passed else RunStatus.COMPLETE
    session.add(run)
    audit.record(
        session, "run.verified" if report.passed else "run.verification_failed",
        summary=(
            f"run {run_id} verification {'passed' if report.passed else 'FAILED'}: "
            f"{report.totals['passed']}/{report.totals['files']} files clean"
        ),
        actor=actor, run_id=run_id, payload=report.totals,
    )

    if write_outputs:
        settings.out_dir.mkdir(parents=True, exist_ok=True)
        (settings.out_dir / "verification.json").write_text(
            json.dumps(report.to_dict(), indent=2)
        )
        (settings.out_dir / "verification.md").write_text(render_markdown(report, rulebook))

    session.commit()
    return report


def _verify_document(
    session: Session,
    store: ObjectStore,
    run: Run,
    document: Document,
    rulebook: Rulebook,
    settings: Settings,
    decisions: dict[int, Decision],
    *,
    actor: str,
    write_outputs: bool,
) -> FileVerification | None:
    """Verify one document. Returns None for documents the run never redlined."""
    ingested = documents.latest_at_stage(session, document.id, Stage.INGESTED)  # type: ignore[arg-type]
    redlined = documents.latest_at_stage(session, document.id, Stage.REDLINED)  # type: ignore[arg-type]
    if ingested is None:
        return None

    result = FileVerification(name=document.name)

    changes = session.exec(
        select(Change)
        .where(Change.run_id == run.id, Change.document_id == document.id)
        .order_by(Change.id)
    ).all()

    source = documents.content(store, ingested)

    adjudicated_keeps: set[tuple[str, int, str]] = set()

    if redlined is None or not changes:
        # A clean document: nothing was proposed, so the original is already final.
        final_data = source
    else:
        resolutions: list[tuple[Sequence[int], DecisionKind, str | None]] = []
        expected_by_location: dict[tuple[str, int], list[tuple[str, str]]] = {}

        for change in changes:
            decision = decisions.get(change.id)  # type: ignore[arg-type]
            if decision is None:
                result.undecided.append({
                    "change_id": change.id,
                    "rule_id": _rule_of(session, change),
                    "mechanism": change.mechanism.value,
                    "original": change.original_text,
                    "proposed": change.proposed_text,
                })
                continue

            # A comment-only change proposed no edit: the judge recommended keeping the
            # deprecated term. The reviewer's decision is what adjudicates it. Accepting
            # ratifies the keep, so the hit the scanner will keep finding is accounted
            # for; rejecting means the reviewer wants a fix nobody has made yet, which
            # must block the gate rather than pass quietly.
            if not change.revision_ids and change.revision_id is None:
                location = _location_of(session, change)
                rule_id = _rule_of(session, change)
                if decision.decision is DecisionKind.ACCEPTED:
                    result.kept += 1
                    if location is not None:
                        adjudicated_keeps.add((location[0], location[1], rule_id))
                else:
                    result.disputed.append({
                        "change_id": change.id,
                        "rule_id": rule_id,
                        "original": change.original_text,
                        "reviewer": decision.reviewer,
                        "note": decision.note,
                        "problem": "reviewer rejected the recommendation to keep, "
                                   "but no replacement has been applied",
                    })
                continue

            resolutions.append(
                (change.revision_ids or ([change.revision_id] if change.revision_id is not None else []),
                 decision.decision, decision.final_text)
            )

            if decision.decision is DecisionKind.ACCEPTED:
                result.accepted += 1
                applied_text = change.proposed_text
            elif decision.decision is DecisionKind.EDITED:
                result.edited += 1
                applied_text = decision.final_text or change.proposed_text
            else:
                result.rejected += 1
                continue  # rejected: the text does not change, nothing to explain

            if change.mechanism.value == "ai":
                result.ai += 1
            else:
                result.deterministic += 1

            location = _location_of(session, change)
            if location is not None:
                expected_by_location.setdefault(location, []).append(
                    (change.original_text, applied_text)
                )

        if result.undecided or result.disputed:
            result.passed = False
            return result

        redlined_data = documents.content(store, redlined)
        final_data = apply_decisions(redlined_data, resolutions)

        # --- unexplained-edit check ---------------------------------------
        before = paragraph_texts(source, document.name)
        after = paragraph_texts(final_data, document.name)
        for key in sorted(set(before) | set(after)):
            old, new = before.get(key, ""), after.get(key, "")
            if old == new:
                continue
            if not explain_difference(old, new, expected_by_location.get(key, [])):
                result.unexplained.append({
                    "part": key[0],
                    "paragraph_index": key[1],
                    "before": old[:240],
                    "after": new[:240],
                    "explained_by": len(expected_by_location.get(key, [])),
                })
        result.diff = _render_diff(document.name, before, after)

    # --- re-scan the final text ------------------------------------------
    # A term a reviewer explicitly decided to keep will be found again on every scan -
    # that is correct behaviour, not a failure. Such a hit passes the gate only when a
    # ratified keep decision covers its exact location and rule; every other hit is
    # outstanding and fails.
    remaining = scan_bytes(final_data, document.name, rulebook)
    for hit in remaining:
        entry = {
            "rule_id": hit.rule_id, "matched": hit.matched_text, "part": hit.part,
            "location": hit.location.describe(), "classification": hit.classification,
        }
        key = (hit.location.part, hit.location.paragraph_index, hit.rule_id)
        if key in adjudicated_keeps:
            result.adjudicated.append(entry)
        else:
            result.hits.append(entry)

    result.passed = not (
        result.hits or result.undecided or result.disputed or result.unexplained
    )

    # --- store the final version -----------------------------------------
    stage = Stage.VERIFIED if result.passed else Stage.FINAL
    version = documents.add_version(
        session, store, document, final_data, stage,
        actor=actor, actor_kind=ActorKind.HUMAN,
        parent=redlined or ingested, run_id=run.id,
        note=("verified clean" if result.passed
              else "; ".join(result.failure_reasons) or "verification failed"),
        summary={
            "accepted": result.accepted, "rejected": result.rejected,
            "edited": result.edited, "remaining_hits": len(result.hits),
            "unexplained_edits": len(result.unexplained),
        },
    )
    result.final_version_id = version.id
    result.final_sha256 = version.content_sha256

    audit.record(
        session,
        "document.verified" if result.passed else "document.verification_failed",
        summary=f"{document.name}: "
                + ("clean" if result.passed else "; ".join(result.failure_reasons)),
        actor=actor, actor_kind=ActorKind.HUMAN, run_id=run.id,
        document_id=document.id, document_version_id=version.id,
        content_sha256=version.content_sha256,
        payload=result.to_dict(),
    )

    if write_outputs:
        (settings.final_dir / f"{Path(document.name).stem}.final.docx").write_bytes(final_data)

    return result


def _rule_of(session: Session, change: Change) -> str:
    from termguard.models import Hit as HitRow

    hit = session.get(HitRow, change.hit_id) if change.hit_id else None
    return hit.rule_id if hit else ""


def _location_of(session: Session, change: Change) -> tuple[str, int] | None:
    from termguard.models import Hit as HitRow

    hit = session.get(HitRow, change.hit_id) if change.hit_id else None
    if hit is None:
        return None
    return hit.part, hit.paragraph_index


def _render_diff(
    name: str, before: dict[tuple[str, int], str], after: dict[tuple[str, int], str]
) -> str:
    """A readable per-paragraph diff, for the report appendix."""
    import difflib

    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, ""), after.get(key, "")
        if old == new:
            continue
        lines.append(f"@@ {name} {key[0]} paragraph {key[1]} @@")
        for line in difflib.unified_diff([old], [new], lineterm="", n=0):
            if line.startswith(("---", "+++", "@@")):
                continue
            lines.append(line)
    return "\n".join(lines)


# -------------------------------------------------------------------- report


def render_markdown(report: VerificationReport, rulebook: Rulebook) -> str:
    """The one-page verification summary."""
    totals = report.totals
    verdict = "PASS" if report.passed else "FAIL"
    lines = [
        f"# TermGuard verification - run {report.run_id}",
        "",
        f"**Result: {verdict}**",
        "",
        "| | |",
        "| --- | --- |",
        f"| Run | {report.run_id} |",
        f"| Verified at | {report.verified_at} |",
        f"| Rulebook hash | `{report.rulebook_hash}` ({len(rulebook)} rules) |",
        f"| Corpus hash | `{report.corpus_hash}` |",
        f"| Files | {totals['files']} ({totals['passed']} clean, {totals['failed']} failing) |",
        f"| Reviewers | {', '.join(report.reviewers) or 'none recorded'} |",
        "",
        "## Changes by mechanism",
        "",
        "| Mechanism | Applied |",
        "| --- | ---: |",
        f"| Deterministic (rule engine) | {totals['deterministic']} |",
        f"| AI-proposed | {totals['ai']} |",
        f"| **Total applied** | **{totals['deterministic'] + totals['ai']}** |",
        "",
        "## Reviewer decisions",
        "",
        "| Decision | Count |",
        "| --- | ---: |",
        f"| Accepted | {totals['accepted']} |",
        f"| Edited | {totals['edited']} |",
        f"| Rejected | {totals['rejected']} |",
        f"| Kept (ratified exception) | {totals['kept']} |",
        f"| Undecided (blocking) | {totals['undecided']} |",
        f"| Disputed keeps (blocking) | {totals['disputed']} |",
        "",
        "## Gate checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
        f"| Every change decided | {'PASS' if totals['undecided'] == 0 else 'FAIL'} "
        f"({totals['undecided']} undecided) |",
        f"| Every remaining term adjudicated | "
        f"{'PASS' if totals['remaining_hits'] == 0 else 'FAIL'} "
        f"({totals['remaining_hits']} unadjudicated) |",
        f"| No disputed keeps outstanding | {'PASS' if totals['disputed'] == 0 else 'FAIL'} "
        f"({totals['disputed']} disputed) |",
        f"| No unexplained edits | {'PASS' if totals['unexplained_edits'] == 0 else 'FAIL'} "
        f"({totals['unexplained_edits']} found) |",
        "",
    ]

    if totals["adjudicated_exceptions"]:
        lines += [
            "## Ratified exceptions",
            "",
            f"{totals['adjudicated_exceptions']} deprecated term(s) remain in the final",
            "documents because a reviewer explicitly decided to keep them - patient-facing",
            "plain language, quoted regulatory text, and the like. Each one is a recorded",
            "decision in the audit log, not an oversight. They are listed per file below.",
            "",
            "| File | Rule | Term | Location |",
            "| --- | --- | --- | --- |",
        ]
        for item in report.files:
            for entry in item.adjudicated:
                lines.append(
                    f"| {item.name} | {entry['rule_id']} | `{entry['matched']}` "
                    f"| {entry['location']} |"
                )
        lines.append("")

    failing = [f for f in report.files if not f.passed]
    if failing:
        lines += ["## Failing files", ""]
        for item in failing:
            lines.append(f"### {item.name}")
            lines.append("")
            for reason in item.failure_reasons:
                lines.append(f"- {reason}")
            for hit in item.hits[:10]:
                lines.append(f"  - remaining: `{hit['rule_id']}` {hit['matched']!r} at {hit['location']}")
            for edit in item.unexplained[:10]:
                lines.append(
                    f"  - unexplained edit in {edit['part']} paragraph "
                    f"{edit['paragraph_index']}: {edit['before'][:80]!r} -> {edit['after'][:80]!r}"
                )
            for undecided in item.undecided[:10]:
                lines.append(
                    f"  - undecided: change {undecided['change_id']} "
                    f"({undecided['rule_id']}) {undecided['original']!r} -> {undecided['proposed']!r}"
                )
            lines.append("")
    else:
        lines += [
            "## Files",
            "",
            "| File | Accepted | Edited | Rejected | Final SHA-256 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for item in report.files:
            sha = (item.final_sha256 or "")[:12]
            lines.append(
                f"| {item.name} | {item.accepted} | {item.edited} | {item.rejected} | `{sha}` |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "Every change above is recorded in the audit log with its mechanism, the rule that",
        "produced it, and the reviewer who decided it. The final documents were re-scanned",
        "with the same rulebook hash shown above.",
        "",
    ]
    return "\n".join(lines)
