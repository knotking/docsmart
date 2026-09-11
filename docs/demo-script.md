# TermGuard demo script

Eleven minutes, one Word-opening moment in the middle. Written so anyone on the team can
give it without having seen the code.

**Before you start**

```bash
make demo          # ~35s: runs the pipeline, auto-accepts, ends on a passing gate
make api           # terminal 2
make web           # terminal 3 -> http://localhost:5173
```

Have these open: the dashboard, a terminal, and a Finder/Explorer window on
`data/out/redlined/`. If you want a clean start mid-demo, `python scripts/demo.py --reset`.

The numbers below are what the shipped corpus produces. If they differ, read the ones on
screen — never the ones in this document.

---

## 0:00 — Where things stand (60 seconds)

Open **`/metrics`** — the landing screen.

> "Before the demo proper: this is what someone opening the tool sees. Documents under
> management, how many are verified clean, what is waiting on a decision. And the two
> numbers that decide whether this stays switched on."

Point at **"How much a machine decided"**.

> "A third of the decisions on the last run were made by an agent, not a person — and
> every one of them names the policy clause that authorized it. None are unattributed;
> if any were, that box would be red, because a machine decision with no authority behind
> it is a defect in the control, not a statistic."

Then **"Trust in the model's proposals"**.

> "And this is the honest measure of the AI. Not its confidence — what reviewers actually
> did with its suggestions. If that override rate climbs, a rule's context note is wrong,
> and we find out here rather than in an audit."

Then move to the story.

---

## 1:00 — The problem, in one sentence

> "A regulated document set has to use the same words everywhere. When a device is
> renamed, or a term changes meaning to a regulator, someone has to find every occurrence
> across a few hundred Word files — including the ones in running headers and footnotes —
> change them in a way a reviewer can audit, and then prove the whole set is consistent.
> Today that is a person with Find and Replace and a spreadsheet."

Open **`/rulebook`**.

> "This is the spreadsheet, turned into something executable. Twelve rules here. Look at
> what a rule carries beyond old-term/new-term: an approved term, yes, but also
> **exceptions** — spans that match but must never be changed, like quoted CFR text — and
> a **context** flag, which says a match is never enough on its own and a human has to
> decide."

Point at **R-002**, `side effect → adverse event`, context flag on.

> "This is the one that makes the point. In a clinical evaluation, 'adverse event' is
> required. In the patient-facing section of an instruction leaflet, 'side effect' is the
> readability-tested wording and changing it is a regulatory problem. Same words, opposite
> answers. A find-and-replace cannot tell them apart. Hold onto that — we come back to it."

---

## 2:15 — Start a run

Go to **`/run`**, click **Start run**, narrate the progress log as it scrolls.

> "Twenty-six documents. For each one it reaches every paragraph — body, headings, tables,
> headers, footers, footnotes — finds the rule matches, writes the mechanical ones as
> tracked changes, and sends the judgment calls to the model."

When the summary lands, go straight to the two bars.

> "**Top bar: 286 hits. 236 unambiguous, 50 needing judgment.** That split is the product.
> Eighty-two percent is a mechanical substitution that code can do and a human can approve
> in bulk. Eighteen percent needs a decision, and the tool's job there is to *not* guess."
>
> "**Second bar: who actually made each change — 236 by the rule engine, 28 by the
> model.** You can always answer 'how much did the AI decide', and the answer is a number
> you can point at, on every screen, in the Word file, and in the audit export."

Now the **hits by part** chart.

> "And here is the one that lands with anyone who has done this manually. **50 of the 286
> hits are not in the body.** Twenty-four in running headers, fourteen in footers, twelve
> in footnotes. The old product name is in the header of every page of every instruction
> document. That is what a manual pass misses, and it is what gets caught at the wrong
> moment."

---

## 4:15 — Open it in Word

Download **`RMS-001.redlined.docx`** from the file list — or open
`data/out/redlined/RMS-001.redlined.docx` directly. This one file carries every kind of
change.

Open it in Word and turn on the **Review** pane, **All Markup**.

Four things to show, in this order:

1. **A rule-engine change in the body.** `Meridian Pump 2 → Meridian Infusion System`.
   > "An ordinary tracked change. Author: *TermGuard (rule engine)*. Right-click, Reject,
   > and it is gone. Nothing about this is unusual to a reviewer — which is the point."

2. **The comment.** Click it.
   > "Every change cites the rule that produced it, the rationale, and the mechanism.
   > *R-001 … Mechanism: deterministic.* The reviewer never has to ask why."

3. **The header and the footnote.** Scroll to the page header, then the footnote at the
   bottom.
   > "'labelling' to 'labeling' in the running header. In the footnote, '250 ml' to
   > '250 mL' and 'physician' to 'healthcare provider'. Same treatment, same audit trail.
   > These are the ones nobody finds by reading."

4. **The AI-proposed change.** Find the `side effect → adverse event` change.
   > "Different author: *TermGuard (AI-proposed)*. The comment carries the model's
   > one-sentence justification, the model id, and 'Requires reviewer decision'. In Word
   > you can filter the Review pane by author — so a reviewer can look at only the AI's
   > proposals, separately, which is usually the first thing they ask to do."

Then open **`IFU-001.redlined.docx`** for the contrast:

> "Same rule, R-002. Here the model said **keep** — this is the 'What you may experience'
> section, it addresses the patient directly. So there is no tracked change at all: just a
> comment recording that it was considered and why it was left alone."

And, in the same file, the Regulatory Note paragraph with the quoted CFR text:

> "One more. This sentence quotes the regulation verbatim — *'the physician shall maintain
> records'*. It contains two terms the rulebook wants changed, and neither was touched:
> the rule's exception pattern suppressed the match before it ever became a hit. It is not
> a change that was rejected; it was never proposed."

---

## 6:45 — Who is doing the work

Open **`/workflow`** briefly.

> "Several people work a run together, and one of the participants is an agent. The policy
> table is the important thing here: it says exactly what the agent may decide on its own
> — spelling, unit style, hyphenation — and the rationale for each grant. The three
> context-dependent rules are deliberately absent. The agent may *propose* on those; it
> may never dispose of them."

Scroll to **Sign-off**.

> "And this is the two-person rule. Dana can approve this run. Alice cannot — she reviewed
> ninety-five changes in it. The tool tells you *why* rather than hiding the button,
> because a control you can't see is one people assume is broken."

---

## 7:15 — The reviewer queue

Back to the dashboard, **`/review`**.

> "This is the screen that gets used. Left: everything awaiting a decision, filterable by
> file, rule, or mechanism. Right: the change in context — the sentence with the deletion
> struck and the insertion highlighted, the paragraph around it, the rule, and for AI
> proposals the model and its reasoning."

Filter **mechanism = AI-proposed**.

> "A reviewer who only trusts themselves on the AI's work can look at just those."

Now clear about ten items with the keyboard: `j`/`k` to move, `a` to accept, `r` to reject.

> "Keyboard-driven, because the volume is the problem. Roughly two seconds a decision."

Reject one deliberately, and say why:

> "I'm rejecting this one. Watch what that does to the gate in a moment."

Then click **Edit** on another, change the wording, and save.

> "And a reviewer can override the wording entirely. That final text is what goes into the
> document, and it is recorded as *edited* — distinct from accepted — in the audit trail."

---

## 8:45 — Verify

Go to **`/verify`**, click **Run verification**.

If you rejected a genuine violation above, it fails. Show that first:

> "Fails. And it names the file and the term still in it. The gate applies every decision,
> rebuilds each document as it would be if approved, and re-scans it with the same
> rulebook. A rejected violation is still a violation, so it will not pass."

Accept the outstanding item, re-run, and get the pass.

> "Four checks. Every change decided. Every remaining term adjudicated. No disputed keeps.
> No unexplained edits."

Point at **ratified exceptions**.

> "This one matters. Twenty-two deprecated terms are still in the final documents — the
> patient-facing 'side effect' occurrences. The scanner still finds them, and it always
> will. They pass because a reviewer explicitly decided to keep them, and each one is
> listed here with the decision behind it. 'Zero violations' would have been a lie; this
> is the honest version."

Now the **unexplained edit** check. In the terminal:

```bash
python scripts/demo.py --reset --inject-stray-edit
```

> "This runs the same pipeline, then hand-edits one document after review — the way
> someone would with the file open in Word — and re-runs the gate."

It fails: `SOP-001.docx: 1 unexplained edit(s)`.

> "Every paragraph that differs between the original and the final document has to be
> explained by a decision. One wasn't. That is the backstop: it does not just prove the
> documents are consistent, it proves the tool changed only what it said it changed."

---

## 10:00 — The audit trail

Click **Download audit CSV**. Open it.

> "One row per change. The document, the location down to the table cell, the rule, the
> mechanism, the model and prompt hash where it was the AI, the content hash of the
> version it landed in, the reviewer, and their decision."

Close on:

> "Every change, what made it, and who approved it."

If there is time, show **`/documents`**:

> "And every document keeps its whole life. Three versions here — as received, redlined,
> as approved — each with a content hash. Any of them downloads exactly as it was. That
> button re-hashes all seventy-six stored versions and confirms nothing has been altered
> since it was written."

---

## The two questions you always get

### "What if the AI is wrong?"

Three answers, in this order:

1. **It is confined.** It only ever sees hits the scanner already classified as needing
   judgment — 50 of 286 here. It gets one sentence, one paragraph of context, and one
   rule. It never sees the document and cannot reach anything outside that sentence.
2. **Its output is checked by code, not trusted.** The revised sentence is token-diffed
   against the flagged span. Anything changed outside that span plus a two-token margin is
   rejected as an over-edit and escalated to a human — however sensible the rewrite looks.
   Same for a malformed response, or a "change" that does not contain the approved term.
   We test this with deliberately bad model responses in the suite.
3. **It never decides anything.** Every AI proposal is a tracked change marked "requires
   reviewer decision", and the verification gate will not pass a run with an undecided
   change in it.

Then, if pressed: *"and if it is wrong in a way all three miss, it is a tracked change by
a named author that a reviewer rejects in one click, and the rejection is in the audit
log."*

### "How do you validate this?"

Two different answers, because they are two different systems:

- **The deterministic pipeline is measured.** `make test` generates 26 documents with 308
  deliberately planted violations — 236 that must be changed, 50 that must reach a human,
  22 protected by exceptions that must never be touched — and measures against that ground
  truth. Recall 1.0, precision 1.0, zero false positives on the clean control documents.
  The build fails if any of those slip. Run `make test` in front of them if they want it.
- **The AI step is validated by containment and by human review**, not by output accuracy,
  because output accuracy is not something either of us can certify. The tests assert that
  bad model output is *rejected*, not that good model output is produced.
- **And it is measured in production.** The metrics screen tracks what reviewers did with
  the model's proposals, per rule. That is the number to watch: if reviewers start
  overruling a rule, you find out from the dashboard rather than from an audit finding.

> "Which is the honest split: the part that can be proven correct, we prove. The part that
> cannot, we contain and put a human in front of."

---

## If someone asks to see it on their documents

```bash
TERMGUARD_CORPUS_DIR=/their/folder python scripts/scan.py --report-only
```

> "Read-only. It writes nothing, changes nothing, and never calls the model. You get the
> hit counts, the split by document part, and an estimate of the reviewer hours. That is
> the right first step, and it can run inside your network."
