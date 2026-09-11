import { useState } from "react";
import { Badge } from "./components";
import ComparisonSection from "./ComparisonSection";

/**
 * User documentation, for the people who work the queue rather than the people who
 * deploy it. Written to answer the questions reviewers actually ask on their first day:
 * what am I looking at, what do these badges mean, when do I reject versus hand off, and
 * why won't it let me approve this.
 */
export default function HelpPage() {
  const [tab, setTab] = useState("using");

  return (
    <div className="help">
      <h1>{tab === "using" ? "How to use TermGuard" : "How TermGuard compares"}</h1>
      <p className="sub">
        {tab === "using"
          ? "For reviewers, team leads and approvers. Everything here is about working the system, not running it."
          : "Where this sits against the tools you may already have, and where it falls short of them."}
      </p>

      <div className="tabs">
        <button className={tab === "using" ? "tab active" : "tab"} onClick={() => setTab("using")}>
          Using TermGuard
        </button>
        <button
          className={tab === "compare" ? "tab active" : "tab"}
          onClick={() => setTab("compare")}
        >
          How it compares
        </button>
      </div>

      {tab === "compare" ? <ComparisonSection /> : <UsingSection />}
    </div>
  );
}

function UsingSection() {
  return (
    <>

      <div className="card">
        <ul className="help-toc">
          <li><a href="#what">What TermGuard does</a></li>
          <li><a href="#role">Which role you have</a></li>
          <li><a href="#sources">Adding rules from a document</a></li>
        <li><a href="#queue">Working the review queue</a></li>
          <li><a href="#decide">Accept, reject or reword</a></li>
          <li><a href="#move">When to move a change instead</a></li>
          <li><a href="#away">Going away</a></li>
          <li><a href="#word">Reading the file in Word</a></li>
          <li><a href="#gate">Verification and sign-off</a></li>
          <li><a href="#metrics">Reading the metrics</a></li>
          <li><a href="#faq">Common questions</a></li>
        </ul>
      </div>

      <h2 id="what">What TermGuard does</h2>
      <p className="lead">
        It reads a folder of Word documents, finds every place the wording does not match
        the approved terminology, and proposes each fix as an ordinary Word tracked change
        with a comment citing the rule.
      </p>
      <p>
        It does not change anything on its own. Every proposal waits for a person, and the
        run cannot be signed off until each one has a decision. Your original files are
        never touched — the tool works on copies and keeps every version it produces.
      </p>
      <p>
        It looks everywhere, not just in the body text: headings, table cells, running
        headers and footers, and footnotes. On a typical document set roughly one hit in
        five is somewhere a person reading the document would not have looked.
      </p>

      <h2 id="role">Which role you have</h2>
      <div className="help-role">
        <strong>Reviewer</strong> — you decide individual changes. Most people are this.
      </div>
      <div className="help-role">
        <strong>Team lead</strong> — a reviewer who is also where your team's escalations
        land. If a colleague cannot decide something, it comes to you.
      </div>
      <div className="help-role">
        <strong>Approver</strong> — you sign off a whole run once every change is decided
        and verification has passed. You cannot approve a run you reviewed changes in
        (see <a href="#gate">verification and sign-off</a>).
      </div>
      <p>
        You may hold more than one. Your teams decide which work reaches you: each rule
        belongs to a team, and changes for that rule go to that team's queue. Check{" "}
        <strong>Teams</strong> in the sidebar to see who owns what.
      </p>

      <h2 id="sources">Adding rules from a document</h2>
      <p>
        On <strong>Sources</strong>, drop in a style guide, terminology SOP, glossary
        screenshot or recorded training session — or paste the address of a web page.
        TermGuard reads it and proposes rules from what it finds.
      </p>
      <p>
        <strong>Nothing it finds is in force until you accept it.</strong> Each proposal
        shows the sentence it came from, and that sentence is what you are reviewing —
        not the rule. A proposed rule can look entirely sensible and still be backwards,
        and a backwards rule does not produce an error: it changes correct wording into
        wrong wording in every document, confidently. Read the quote.
      </p>
      <p>Three ways to respond:</p>
      <ul>
        <li><strong>Add rule</strong> — the rule reads the sentence correctly.</li>
        <li>
          <strong>Correct it</strong> — the right rule is in there but the wording is off.
          Fix the terms and add it. It is recorded as edited rather than accepted, because
          you wrote it, not the extractor.
        </li>
        <li><strong>Reject</strong> — not a rule, or not one you want.</li>
      </ul>
      <div className="help-note">
        <strong>Warnings on a proposal are worth reading.</strong> "Already covered" means
        an existing rule says the same thing. <strong>"CONFLICT"</strong> means an existing
        rule says something <em>different</em> about the same term — accepting it would
        leave the rulebook telling the scanner two things at once. Fix the disagreement
        before adding it.
      </div>
      <p>
        Some sources cannot be read fully, and the list says which and why — an image needs
        a vision model, a video needs captions or a transcript. A source that could not be
        read is marked as such rather than shown as one containing no terminology.
      </p>
      <p>
        Adding a rule changes what "correct" means, so runs made before it can no longer be
        verified against the current rulebook. The page tells you which runs those are.
        Start a new run to apply what you have added.
      </p>

      <h2 id="queue">Working the review queue</h2>
      <p>
        Open <strong>Review</strong>. The left column is everything waiting on you — your
        own assignments, your teams' pools, and anything you are covering for a colleague
        who is away. The right pane shows the change in context.
      </p>
      <p>It is built to be worked with the keyboard:</p>
      <ul>
        <li><span className="keycap">j</span> / <span className="keycap">k</span> — next / previous</li>
        <li><span className="keycap">a</span> — accept</li>
        <li><span className="keycap">r</span> — reject</li>
        <li><span className="keycap">e</span> — reword it yourself</li>
      </ul>
      <p>
        The list advances as you decide, so you can clear a queue without touching the
        mouse. Two seconds a change is a normal pace.
      </p>
      <div className="help-note">
        <strong>You will not collide with a colleague.</strong> Opening a change takes a
        short hold on it. If somebody else already has it, the system says so rather than
        letting you both decide and quietly keeping the last answer. Holds expire on their
        own, so nothing is stuck if a colleague closes their laptop.
      </div>

      <h3>What the badges mean</h3>
      <p>
        Every change says who proposed it, and this is the distinction to pay attention
        to:
      </p>
      <ul>
        <li>
          <Badge kind="rule">Rule engine</Badge> — a mechanical substitution made by code.
          "labelling" to "labeling", "ml" to "mL". These are safe to work through quickly.
        </li>
        <li>
          <Badge kind="ai">AI-proposed</Badge> — a language model read the sentence and
          suggested this. It only ever sees one sentence and one rule, never the document,
          and its suggestion was checked by code before it reached you. It still needs your
          judgement. The model's own reasoning is shown so you can disagree with it.
        </li>
      </ul>
      <p>
        You can filter the queue to just AI proposals if you would rather look at those
        separately — many reviewers do on their first few runs.
      </p>

      <h2 id="decide">Accept, reject or reword</h2>
      <ul>
        <li>
          <strong>Accept</strong> — the proposed wording is right. The tracked change stays
          in the document.
        </li>
        <li>
          <strong>Reject</strong> — the term should stay as it is, or the replacement is
          wrong here. The tracked change is reverted and the original wording remains.
        </li>
        <li>
          <strong>Reword</strong> — the term needs changing but not the way it was
          proposed. Your wording is what goes into the document, and the record shows it
          was edited rather than accepted.
        </li>
      </ul>
      <div className="help-note">
        Rejecting is not the same as leaving it alone. If a genuine violation is rejected,
        verification will fail and name it — so reject when the flagged text is correct as
        written, not to skip something you are unsure about. For that, hand it on.
      </div>

      <h2 id="move">When to move a change instead</h2>
      <p>
        If you cannot decide something, do not leave it. An undecided change looks exactly
        like one nobody has opened yet, which is how the hard cases end up sitting until
        the deadline. Use one of these instead — each asks you for a reason, and that
        reason goes on the permanent record.
      </p>
      <ul>
        <li>
          <strong>Escalate</strong> — you cannot make the call. It goes to your team lead
          and is visibly marked escalated.
        </li>
        <li>
          <strong>Hand off</strong> — somebody else should own this. Another person, or
          another team. Pick a person and it stays with them.
        </li>
        <li>
          <strong>Return</strong> — something needs answering before anyone can decide.
          Ask the question and the change leaves the queue until it is answered. Whoever
          answers uses <strong>Answer return</strong>, and it comes back.
        </li>
      </ul>
      <p>
        The right pane shows <em>How it got here</em> for anything that has moved, so you
        can see who touched it before you and why.
      </p>

      <h2 id="away">Going away</h2>
      <p>
        On the <strong>Teams</strong> page, set cover: choose who is away and who is
        covering. Their queue flows to the stand-in and flows back when the cover ends.
      </p>
      <p>
        Nothing is reassigned, so nothing has to be reassigned back — which is how work
        gets lost. If your stand-in also goes away and hands to somebody else, your work
        follows the chain to whoever is actually there.
      </p>
      <p>An agent can never cover for a person. An absence does not become automation.</p>

      <h2 id="word">Reading the file in Word</h2>
      <p>
        From <strong>Run</strong>, download the redlined copy of any document and open it
        in Word with the Review pane on, All Markup. Everything appears as ordinary
        tracked changes — nothing proprietary, nothing you need a plugin to read.
      </p>
      <ul>
        <li>
          Changes by <em>TermGuard (rule engine)</em> are the mechanical ones; those by{" "}
          <em>TermGuard (AI-proposed)</em> are the model's. You can filter the Review pane
          by author to see them separately.
        </li>
        <li>Each comment cites the rule, its rationale, and which mechanism produced it.</li>
        <li>
          Some sentences are deliberately untouched. Where a rule has an exception — quoted
          regulatory text, a historical product name — nothing is proposed at all. That is
          not something that was rejected; it was never raised.
        </li>
      </ul>
      <p>
        Deciding in Word is not the same as deciding here. Accepting a change in Word does
        not record a decision — work the queue in the app, and use Word for reading the
        document in context.
      </p>

      <h2 id="gate">Verification and sign-off</h2>
      <p>
        Once the queue is clear, <strong>Verify</strong> rebuilds each document as it would
        be if approved, re-reads it with the same rulebook, and checks four things:
      </p>
      <ul>
        <li>every change has a decision;</li>
        <li>every deprecated term still present has a decision explaining why;</li>
        <li>nothing is waiting on an unanswered question;</li>
        <li>
          no text differs from the original in a way no decision accounts for — this is
          what proves the tool changed only what it said it changed.
        </li>
      </ul>
      <p>
        Terms you deliberately kept are listed as <em>ratified exceptions</em>, with the
        decision behind each. The report will not claim zero violations when there are
        terms still present; it says which, and why they are allowed.
      </p>
      <div className="help-note">
        <strong>Why you might not be able to approve.</strong> Whoever reviewed changes in
        a run cannot be the person who signs it off — two different people, deliberately.
        The <strong>Workflow</strong> page shows who is eligible and, for everyone else,
        the exact reason they are not. It is a control, not a fault.
      </div>

      <h2 id="metrics">Reading the metrics</h2>
      <p>
        <strong>Metrics</strong> is the landing screen. Four things worth looking at:
      </p>
      <ul>
        <li>
          <strong>Needs attention</strong> — the short list of things to act on. Usually
          empty.
        </li>
        <li>
          <strong>Who recorded each decision</strong> — how much was handled automatically
          against how much reached a person. Automatic decisions are limited to a written
          policy, and each one names the clause that permitted it.
        </li>
        <li>
          <strong>Reviewers upheld / overruled the model</strong> — what your team actually
          did with the AI's suggestions. This is the number to watch.
        </li>
        <li>
          <strong>Rule health</strong> — which rules get overturned most. A rule your team
          keeps rejecting usually has a wrong approved term or unclear guidance. Tell
          whoever owns that rule; it is a rulebook problem, not a reviewer problem.
        </li>
      </ul>
      <p>
        Where a figure says "no data" it means nothing has been measured yet, which is not
        the same as zero.
      </p>

      <h2 id="faq">Common questions</h2>

      <h3>It flagged something that is correct as written.</h3>
      <p>
        Reject it, and say why in the note. If the same rule keeps producing wrong
        proposals, it will show up on Rule health and the rule itself needs fixing.
      </p>

      <h3>The same term is right in one document and wrong in another.</h3>
      <p>
        That is expected, and it is why those cases come to you instead of being changed
        automatically. "Side effect" belongs in patient-facing plain language and "adverse
        event" in a clinical report. Judge by who the section is written for.
      </p>

      <h3>Can I undo a decision?</h3>
      <p>
        Yes — decide it again. The new decision supersedes the old one, and both stay on
        the record showing that it was reconsidered and by whom. Nothing is ever erased.
      </p>

      <h3>Someone else already decided something I disagree with.</h3>
      <p>
        Record your decision over it and say why. The history shows both. If it is
        contentious, escalate instead so a lead settles it.
      </p>

      <h3>What happens to the original documents?</h3>
      <p>
        Nothing. They are read once and never written to. Every version the tool produces
        is kept and can be downloaded exactly as it was — see <strong>Documents</strong>.
      </p>

      <h3>How much did the AI actually decide?</h3>
      <p>
        The second bar on Metrics, always. Automatic decisions are confined to a written
        policy — spelling, unit style and similar — and anything context-dependent is
        routed to a person by design, never to a machine.
      </p>
    </>
  );
}
