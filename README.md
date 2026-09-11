# TermGuard

TermGuard takes a folder of Microsoft Word documents and a terminology rulebook, and
returns redlined copies in which every terminology violation is an ordinary Word tracked
change with a comment citing the rule that produced it. Judgment calls — where the
deprecated term may be correct in context — are routed to a reviewer queue instead of
being changed. Nothing is finished until a re-scan of the reviewed output proves the
corpus is clean and that nothing else was touched.

It is built for a regulated (FDA-facing) document set, so the design is shaped less by
what is convenient than by what a quality auditor will ask for.

---

## The four layers

| Layer | What it does | Modules |
| --- | --- | --- |
| **A — Rulebook** | Terminology rules with exceptions, scope, casing and context flags | `rulebook.py` |
| **B — Scan** | Reach every paragraph in every document part; find and classify matches | `walker.py`, `scanner.py` |
| **C — Redline** | Write tracked changes: deterministic ones by code, judgment calls by the LLM | `redline.py`, `ooxml.py`, `judge.py` |
| **D — Review & verify** | Reviewer decisions, the verification gate, the audit trail, the API | `review.py`, `verify.py`, `audit.py`, `api.py` |
| **D — Workflow & metrics** | Participants, assignment, leases, agent authority, sign-off, metrics | `workflow.py`, `policy.py`, `metrics.py` |
| **D — Org & teams** | Organizations, teams, membership, routing, handoffs, cover | `teams.py` |
| **A — Rule intake** | Mine documents, pages, images and video for candidate rules | `sources.py`, `extract.py`, `intake.py` |
| **S — Substrate** | Config, content-addressed storage, schema, document lifecycle | `config.py`, `storage.py`, `db.py`, `models.py`, `documents.py` |

`pipeline.py` runs A→D over a corpus; `web/` is a dashboard that is purely a view over the
API.

---

## Why it is built this way

Six constraints hold the design in place. Each exists because of a specific question a
regulated audience asks.

**1. Never silently rewrite a document.** Every edit is a `w:ins`/`w:del` tracked change
with an anchored comment, and originals are never modified — outputs are new versions.
*Because:* attribution and reversibility. A reviewer can see what was proposed, by whom,
and reject it. An untracked find-and-replace is unreviewable and therefore unusable.

**2. Two edit mechanisms, separately counted.** Deterministic (code) and AI-proposed (LLM)
changes are authored differently in Word, stored with different mechanisms, and reported
separately everywhere.
*Because:* "how much did the AI decide?" is the first question anyone asks, and the answer
has to be a number, not a shrug. In the demo corpus it is 236 deterministic and 28
AI-proposed.

**3. The LLM sees one sentence.** It is invoked only on hits the scanner classified as
`needs_judgment`, receives that sentence plus one paragraph of context and one rule, and
its output is validated by code — token-diffed against the flagged span, rejected if it
edited anything else.
*Because:* containment. The compliance argument is not "the model behaves well", it is
"the model cannot reach anything else, and here is the code that enforces it".

**4. An append-only audit log.** Every scan, proposal, decision and version writes an
immutable event carrying actor, mechanism, rule, model, prompt hash and content hash.
*Because:* 21 CFR Part 11 expects an audit trail that records who did what, when, and
that cannot be quietly edited afterwards.

**5. Scan the whole document structure.** Body, headings, tables, headers, footers,
footnotes, endnotes, text boxes. A hit in a running header counts the same as one in the
body.
*Because:* this is the failure mode of manual review. In the demo corpus, 50 of 286 hits
sit outside the body — a header carrying the old product name on every page is exactly
what a human pass misses.

**6. Verification is a re-scan.** The gate rebuilds the as-accepted document, re-scans it
with the same rulebook hash, and refuses to pass while anything is unresolved.
*Because:* "consistent" is a claim. This makes it a check.

Three more constraints exist for document lifecycle and portability:

**7. An immutable version chain.** Every state change writes a new `DocumentVersion` with
a content hash, its parent, the run that produced it and the actor responsible.

**8. All document bytes go through `ObjectStore`.** Blobs are content-addressed
(`sha256/…`), so identical content is stored once and the address is an integrity proof.

**9. No cloud-specific code outside `storage.py` and `config.py`.** SQLite and Cloud SQL
are the same code path; a local directory and a GCS bucket are the same interface.

And three for working as a team:

**10. An agent's authority is data.** `data/policy.yaml` says which changes a machine may
decide on its own; it is hashed like the rulebook, and every agent decision records the
clause that authorized it.

**11. Separation of duties is enforced.** Whoever reviewed a run cannot sign it off, and
no agent signs anything off.

**12. Decisions cannot silently overwrite each other.** A change being worked on is held
under an expiring lease.

**13–15. Work is routed, and never changes hands silently.** Teams come from the
rulebook's `owner` fields, every movement records who moved it and why, and cover changes
the effective owner rather than reassigning anything.

---

## Working as a team

Several people — and several agents — work one run together.

**Participants** are typed. A human and an agent are not interchangeable, and the audit
trail never blurs them: every decision resolves to a `Participant` row with a kind and a
set of roles. Identity is *asserted*, not proven — authentication is out of scope and IAM
in front of the service is the real boundary. What the table buys is attribution.

**Work is routed and held.** `auto_assign` spreads undecided changes across reviewers;
`by_file` is the default because a reviewer who has read the document makes faster, better
calls on the rest of it than one parachuted into paragraph 14. A reviewer then *claims* a
change under a short lease. Without that, two people on one queue both decide change 412
and the append-only log faithfully records the second superseding the first with nobody
the wiser. Leases expire, so a closed laptop does not block the queue.

**Agents decide only what a written policy permits.**

```yaml
# data/policy.yaml
agent_may_decide:
  - id: P-001
    rules: [R-006, R-007, R-008]     # US spelling, unit style, hyphenation
    classifications: [unambiguous]
    max_risk: low
    rationale: >-
      Orthographic rules with no semantic content. A wrong decision here produces a
      typo, not a regulatory misstatement.
```

Anything no clause covers goes to a human, and that default is not configurable. A clause
may also set `requires_human_confirm`, which lets an agent clear a repetitive batch — the
same header string on 40 pages — while still requiring a person to confirm it before
sign-off. Rules the rulebook marks `context_required` are deliberately absent from every
clause; delegating them would undo the containment argument entirely.

So "on what authority did a machine approve this?" has a specific answer: clause `P-001`
of policy `ef0f2cab`, which says this, approved by quality-assurance.

### Where rules come from

Rules can be written by hand, imported from a spreadsheet, or **mined from a source**.
Upload a style guide, terminology SOP, glossary screenshot or recorded training session —
or point at a web page — and what it says about terminology becomes candidate rules.

```
upload / fetch  →  read (with a locator per line)  →  extract
                →  candidates, each quoting its source sentence
                →  a person accepts, edits or rejects
                →  the rulebook, hash moves, run again
```

**Nothing extracted applies on its own.** An extracted rule is not a suggestion about
wording — it is an instruction that will rewrite text across every document on the next
run, and the dangerous failure is *reversal*: a rule with its terms swapped produces
confident, wrong edits rather than an error. So candidates carry the sentence they came
from, and the reviewer reads the sentence.

Extraction is patterns first, a model only for prose a pattern cannot reach. Not economy —
a pattern match is *quotable*, identical every run, and checkable in a second:

| Source says | Yields |
| --- | --- |
| a glossary table with a `Do not use / Use instead` heading | one rule per row, direction taken from the heading |
| "Use mL, not ml" | `ml → mL` |
| "The term X is deprecated; use Y" | `X → Y` |
| a note saying "depends on context" | the rule, flagged `context_required` |
| a table whose headings don't say which side is which | **nothing** — column order is not a fallback |

Candidates are checked against the rulebook as they are extracted, so a term another rule
already owns is flagged as a duplicate and a term another rule *contradicts* is flagged as
a conflict before anyone accepts it.

Sources are unequal, and the tool says so rather than pretending:

| Source | State |
| --- | --- |
| `.docx`, `.pdf`, `.txt`, `.md` | works |
| web page | works — URL and retrieval time recorded |
| video with `.vtt` / `.srt` captions | works — each cue's timestamp is its locator |
| images, screenshots | needs a vision model; records what it needs and reads nothing |
| video without captions | needs a transcript or a transcription backend |

Accepting a rule moves the rulebook hash, which means runs produced under the old one can
no longer be verified. That is deliberate, and the UI lists exactly which runs it affects.

### Teams, and how work moves

Teams are not a second thing to maintain. Every rule in the rulebook already names an
owner, so those owner strings *are* the team list:

```
Meridian Medical
├─ regulatory-affairs    owns R-001, R-004, R-009
├─ clinical-affairs      owns R-002, R-010
├─ technical-writing     owns R-003, R-006, R-008
├─ quality-assurance     owns R-005, R-007
└─ systems-engineering   owns R-011, R-012
```

`route_by_rule_owner` sends each change to the team that owns its rule, and any member of
that team can claim it. A rule whose owner has no team is *reported*, not dropped into a
default queue — work landing somewhere nobody watches is worse than work that visibly has
nowhere to go.

Four ways a change moves, each recorded with the actor and a required reason:

| Handoff | What it is for |
| --- | --- |
| **Escalate** | A reviewer cannot decide. It goes to the team lead and is visibly *escalated* — not merely undecided, which is how hard cases sit untouched until the deadline. |
| **Reassign** | Another person or team should own it. Naming a person pins it, so a later re-route will not quietly take it back. |
| **Return** | Something needs answering first. The change leaves the review queue until the question is resolved, and both the question and the answer are on the record. |
| **Cover** | Somebody is away. Their queue flows to a named stand-in and flows back when the cover lapses. |

The change itself is never mutated by any of this. `change_state` — *pooled, assigned,
escalated, returned, decided* — is derived from the handoff history, so it cannot drift
from the events that produced it, and a change's whole routing story reads back in order:

```
route     by system  → clinical-affairs   rule owner
escalate  by alice   → bob                cannot tell if this section is patient-facing
reassign  by bob     → carol              regulatory owns the quoted-CFR reading
```

Two refusals worth knowing about. **An agent can never cover for a human** — an absence
must not quietly become machine authority over work a person was meant to see. And
**cover follows chains**: if Alice is covered by Bob and Bob by Carol, Alice's queue is
Carol's problem. Resolving only direct delegations looks correct at every individual hop
while dropping Alice's work on the floor.

**Sign-off is maker-checker.** A run is approved by someone holding the approver role who
recorded no decisions in it. An agent can never sign off, even if granted the role. The
`force` flag skips the readiness checks so an incomplete run can be explicitly *rejected* —
it does not override the two-person rule, and deliberately has no way to.

On the demo corpus this comes out as: the agent disposes of 96 of 286 changes under two
clauses, two reviewers split the remaining 190 by file, one of them confirms the 20 the
policy flagged, and an uninvolved approver signs the run off. Alice, who reviewed 95 of
them, is refused — and the UI shows her why rather than hiding the button.

---

## Metrics

`/metrics` is the landing screen, and it answers four questions:

- **Posture** — documents under management, how many verified clean, decisions pending.
- **How much a machine decided** — the automation rate, and agent decisions broken out by
  the policy clause that authorized each one. Any decision with no clause behind it is
  flagged as an error, because that would be a bug in the control.
- **Trust in the model** — not the model's confidence, which is unfalsifiable, but *what
  reviewers did with its proposals*. Acceptance and override rates over AI-proposed
  changes that got a human verdict. Agent-decided changes are reported separately as
  delegated volume and never folded into an accuracy number: an agent ruling on itself is
  not evidence.
- **Rule health** — per-rule volume and override rate, sorted worst first. A rule whose
  proposals get overturned 40% of the time has a wrong approved term or a wrong context
  note, and that shows up here long before it becomes an incident.

Two deliberate choices: a metric with an empty denominator renders "no data", never 0%,
because those mean different things and a dashboard that draws them identically will
mislead someone. And the review-pace estimate is suppressed entirely unless there is real
elapsed time behind it — a rate extrapolated from a batch script is fiction someone will
plan against.

The categorical palette (rule engine vs AI) is validated for colorblind separation rather
than chosen by eye; the original, prettier pair failed the chroma floor and read as gray
in a thin bar segment, which is exactly where that distinction has to survive.

---

## Document lifecycle

Every document carries its whole history:

```
v1 ingested   sha=861d5f5d2ade   the original, byte-for-byte
v2 redlined   sha=abcbff9bfc55   13 deterministic + 1 AI-proposed change
v3 verified   sha=795f296963d6   reviewer decisions resolved, re-scanned clean
```

From that you can, at any later date:

- **reproduce any past state** — `GET /documents/{id}/versions/{n}/download` returns the
  exact bytes;
- **see what happened between two states** — `GET /documents/{id}/timeline` interleaves
  versions with the events that produced them;
- **attribute every change** — each `Change` row names its mechanism, rule, and (for AI)
  model, prompt version, prompt hash and request id;
- **prove nothing has rotted** — `GET /integrity` re-hashes every stored version against
  its recorded digest.

---

## Quickstart

```bash
make install        # venv, Python deps, npm deps
make corpus         # generate the 26-document synthetic corpus
make test           # the full suite; prints scanner precision and recall
make demo           # pipeline -> agent + two reviewers -> gate -> sign-off
```

`make demo` runs the multi-participant workflow. `python scripts/demo.py --solo` gives the
single-reviewer version instead.

Then, for the dashboard:

```bash
make api            # http://localhost:8000
make web            # http://localhost:5173
```

`make demo` needs no API key: the LLM step runs from recorded fixtures by default.

### Pointing it at real documents

```bash
export TERMGUARD_CORPUS_DIR=/path/to/documents
export TERMGUARD_RULEBOOK=/path/to/rulebook.yaml
python scripts/scan.py --report-only     # scan only: no files written, no LLM calls
python scripts/demo.py                   # the full pipeline
```

`--report-only` is the right first step on a prospect's documents: it produces the hit
counts and a projected reviewer effort without writing anything or calling the model.

To convert an existing terminology spreadsheet into a rulebook:

```bash
python scripts/import_rulebook.py terms.xlsx -o data/rulebook.yaml
```

### Enabling the live LLM step

```bash
export ANTHROPIC_API_KEY=sk-...
export TERMGUARD_LLM_LIVE=1
```

Responses are cached by `(sentence, rule id, prompt hash, model)`, so re-running over an
unchanged corpus makes no API calls.

**Before trusting the judgment step, exercise the prompt for real:**

```bash
python scripts/check_live_judge.py          # two documents, ~7 calls
python scripts/check_live_judge.py --all    # the whole corpus
```

Everything else in this repo runs from recorded fixtures by default, which is right for
tests and for a demo with no key — but it means the *prompt* can be wrong and the whole
suite still passes. The containment around the model is thoroughly tested; none of that
says the model judges well, because until this script runs it has not been asked.

It grades the one case the product's central claim rests on: R-002, `side effect`, which
must become `adverse event` in a clinical evaluation and must be left alone in
patient-facing plain language. Same term, opposite answers, decided only by context. It
writes nothing — no documents, no rows — and exits non-zero if an answer contradicts the
rulebook.

---

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `TERMGUARD_STORAGE` | `local` | `local` or `gcs` |
| `TERMGUARD_BLOB_ROOT` | `data/blobs` | local object store directory |
| `TERMGUARD_GCS_BUCKET` | — | bucket name when storage is `gcs` |
| `TERMGUARD_DB_URL` | `sqlite:///termguard.db` | any SQLAlchemy URL |
| `TERMGUARD_CORPUS_DIR` | `data/corpus` | input documents |
| `TERMGUARD_OUT_DIR` | `data/out` | exported redlined/final copies and reports |
| `TERMGUARD_RULEBOOK` | `data/rulebook.yaml` | rulebook path |
| `TERMGUARD_POLICY` | `data/policy.yaml` | agent-authority policy path |
| `TERMGUARD_LLM_LIVE` | `0` | `1` to call the API instead of using fixtures |
| `ANTHROPIC_MODEL` | `claude-opus-5` | model for the judgment step |
| `ANTHROPIC_API_KEY` | — | required only when `TERMGUARD_LLM_LIVE=1` |

Moving to GCP is these variables plus a deploy — see [`deploy/README.md`](deploy/README.md).

---

## How this is validated

**The deterministic pipeline is measured, not asserted.** `scripts/make_corpus.py`
generates 26 documents with 308 deliberately planted violations and records the ground
truth: 236 that must be changed, 50 that must reach a human, and 22 protected by an
exception that must never be touched. The test suite measures against it and fails the
build on:

- recall below 1.0 on items that should be found;
- any exception-protected span being flagged;
- precision below 0.95;
- any hit classified against its planted intent;
- any hit on the two clean control documents.

Current results: **286 hits, recall 1.0, precision 1.0, zero control false positives.**
The walker is separately asserted to reach 100% of planted locations, including the ones
in headers, footers and footnotes.

**The AI step is validated by containment, not by output quality.**
`tests/fixtures/judge/adversarial.json` holds deliberately bad model responses — a
reworded sentence, a missing approved term, malformed JSON, an unknown decision value, a
"keep" that edits anyway — and each is asserted to be rejected and escalated to a human.
Tests never call the API.

**The verification gate is tested in both directions**: a fully accepted run passes; a
rejected genuine violation fails and names it; and a document hand-edited after review
fails as an *unexplained edit*.

250 tests, 92% coverage overall (walker 93%, scanner 94%, redline 97%, verify 91%).

---

## What this is not

**It is not a replacement for reviewer sign-off.** Every change lands as a proposal. The
deterministic ones are mechanical substitutions a human can approve in bulk; the AI ones
are explicitly marked as requiring a decision. The gate will not pass a run with an
undecided change in it.

It also does not do coverage it was never given: it reads `.docx` only, has no
authentication or multi-tenancy, and does not connect to a document management system.

## Roadmap

- DMS / SharePoint integration for ingest and check-in
- PDF input via extraction with position mapping back to the source
- SSO and per-role permissions (today: Cloud Run IAM in front, nothing inside)
- Structured-content export (DITA / S1000D) for reuse-based authoring
- Alembic migrations, required before the first schema change against real data
- Cloud Tasks for runs beyond a few hundred documents

## Licence and provenance

The corpus, the rulebook and "Meridian Medical" are entirely fictional, generated for this
demo. No client document was used.
