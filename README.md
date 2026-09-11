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
make test           # 250 tests; prints scanner precision and recall
make demo           # the whole pipeline, ending on the verification gate
```

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
