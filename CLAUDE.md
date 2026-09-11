# TermGuard

Standing instructions for this repository. Read this before changing anything.

## PURPOSE

A folder of `.docx` files plus a terminology rulebook go in. Out come redlined copies of
every file in which each terminology violation is a Word tracked change with a comment
citing the rule, a reviewer queue for context-dependent cases, and a verification report
proving zero remaining violations after review.

Every document that enters the system is tracked for its whole life: each ingest, scan,
redline, review decision and finalization produces a new immutable **document version**
with a content hash, so at any point we can answer "what did this file look like then,
what changed, who or what changed it, and who approved it".

## NON-NEGOTIABLE DESIGN CONSTRAINTS

1. **Never silently rewrite a document.** Every edit to a `.docx` is a tracked change
   (`w:ins` / `w:del`) with an anchored comment. Original files are never modified;
   outputs go to a separate directory.
2. **Two edit mechanisms, kept strictly separate and separately counted:** DETERMINISTIC
   (rule-based, code only) and AI-PROPOSED (LLM). The UI and reports must always show how
   many changes came from each.
3. **The LLM is only ever invoked on hits the scanner classified as `needs_judgment`.** It
   never sees or edits the whole document, only the sentence plus one paragraph of
   context. Its output is a minimal rewrite; anything beyond the flagged span is rejected
   by code.
4. **Every action is written to an append-only audit log:** file, location, rule id,
   mechanism, model + prompt version if AI, proposed text, reviewer decision, timestamp,
   actor.
5. **Scanning covers the entire document structure:** body, tables, headers, footers,
   footnotes, endnotes, text boxes. A hit in a header counts the same as a hit in the body.
6. **Verification is a re-scan of the reviewed output using the same scanner.** The
   pipeline is not done until it reports zero hits.

### Added constraints (document lifecycle + portability)

7. **Immutable version chain.** Document bytes are never overwritten. Every state change
   writes a new `DocumentVersion` row (content SHA-256, parent version, stage, producing
   run, actor). `documents.py` owns this; nothing else writes version rows.
8. **All document bytes go through `storage.ObjectStore`.** No pipeline module may call
   `open()` on a document path directly. Blobs are content-addressed
   (`sha256/<hash>.docx`), so identical content is stored once and a hash proves
   integrity. The local backend is a directory; the GCP backend is a GCS bucket. Swapping
   them is configuration, never a code change.
9. **No cloud-specific code outside `storage.py` and `config.py`.** Database access goes
   through SQLModel with a URL from config, so SQLite (local) and Cloud SQL Postgres (GCP)
   are the same code path. Nothing may assume a local filesystem or SQLite dialect.

## STACK

Python 3.11+ (developed on 3.12), FastAPI, SQLite via SQLModel (Postgres-compatible),
`python-docx` for reading, `docx-editor` for writing tracked changes and comments in the
body, raw OOXML for the parts `docx-editor` does not reach, Anthropic Python SDK for the
LLM step, React + Vite frontend, pytest. No other frameworks without asking.

## REPO LAYOUT

```
termguard/        config.py storage.py db.py models.py documents.py
                  rulebook.py walker.py scanner.py ooxml.py redline.py
                  judge.py audit.py verify.py api.py
web/              React app
data/rulebook.yaml
data/corpus/      inputs
data/out/         outputs (redlined/, final/)
data/blobs/       local object store (content-addressed)
tests/ scripts/ deploy/
```

## docx-editor notes

Installed version **0.8.2**. Verified empirically against a probe document, not assumed.

Entry point is `docx_editor.Document`:

```python
import docx_editor as de
doc = de.Document.open(path, author="TermGuard (rule engine)")   # author set at OPEN time
hit = doc.find_text("Meridian Pump 2", paragraph=ref)            # crosses run boundaries
doc.replace(hit, "Meridian Infusion System", note="R-001")       # tracked w:del + w:ins
doc.add_comment(hit, "R-001: ... Mechanism: deterministic.")     # anchored comment
doc.save(out_path)
doc.close()
```

Key API facts (all verified):

- `Document.open(path, author=..., workspace_dir=..., force_recreate=...)` — the revision
  author comes from `open()`, so **one open per author**. A file needing both rule-engine
  and AI-proposed changes is opened twice, in sequence.
- `replace(find, replace_with, *, paragraph=None, occurrence=None, note=None) -> EditResult`
- `add_comment(anchor_text, comment, *, paragraph=None, occurrence=None) -> int`
- `find_text(text, occurrence=0, paragraph=None) -> SearchResult | None` and `find_all(...)`
  — these match **across element/run boundaries**, which solves split-run anchoring for us.
- `list_paragraphs_structured() -> [ParagraphInfo]` with fields
  `index, ref, text, in_table, style, outline_level`. `ref` is a hash-anchored
  `"P{i}#{hash}"` string; passing a stale ref raises `HashMismatchError`, which is how we
  detect a document changing under us.
- `get_paragraph_location(ref) -> ParagraphLocation` with `table, list, style,
  outline_level, heading_path, section`.
- `list_revisions()`, `accept_revision(id)`, `reject_revision(id)`, `accept_all()`,
  `reject_all()` — **we use these to build the as-accepted final document in `verify.py`**
  rather than hand-resolving XML.
- `save(path, validate=False, force=False, track_changes=None)`, then `close()`.
- `compute_paragraph_hash` is exported for anchor validation.

**Reach limits — the reason `ooxml.py` exists.** Verified on a probe document containing a
header, footer, table and heading: `list_paragraphs_structured()` and `get_visible_text()`
returned body and table-cell text only. Header and footer text were absent. The
`ParagraphLocation` docstring confirms header/footer/footnote are not yet modelled, and
the source deliberately excludes `w:txbxContent` (text boxes) to avoid double-listing.

Therefore:

| Part | Engine |
| --- | --- |
| body paragraphs, headings, table cells | `docx-editor` |
| headers, footers, footnotes, endnotes, text boxes | `termguard/ooxml.py` (raw `w:ins`/`w:del` + `comments.xml`) |

Never assume a part is covered; `walker.py` labels every paragraph with its part and
`redline.py` dispatches on that label.

**python-docx note:** version 1.2.0 has no footnote-authoring API (`Paragraph.add_footnote`
does not exist). `scripts/make_corpus.py` writes `word/footnotes.xml` directly.

## CONVENTIONS

- Type hints everywhere.
- Every module has a docstring naming its layer: **A** rulebook, **B** scan, **C** redline,
  **D** review/verify, **S** substrate (config, storage, db, models, documents, audit).
- Tests for the deterministic pipeline are mandatory; coverage of `walker`, `scanner`,
  `redline` and `verify` must stay above 90%.
- The LLM is never called in tests. `judge.py` runs from recorded fixtures unless
  `TERMGUARD_LLM_LIVE=1`.
- Commit after each working step with a conventional-commit message.

## WHAT NOT TO BUILD

No authentication, no multi-tenancy, no DMS integration, no PDF input. These are roadmap
items; mention them in README only.
