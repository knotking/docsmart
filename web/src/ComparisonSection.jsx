import { Badge } from "./components";

/**
 * Where TermGuard sits against adjacent tools.
 *
 * Written to be read by an evaluator, which means the limitations are on the same page as
 * the strengths and stated first where they matter. A comparison that only lists what a
 * product wins at is read as marketing and discounted entirely; one that names its own
 * gaps gets the wins taken seriously.
 *
 * Claims about other products are drawn from their public positioning and documentation,
 * not from operating them, and the page says so rather than asserting what a competitor
 * cannot do as though it were tested fact.
 */
export default function ComparisonSection() {
  return (
    <>
      <div className="help-note">
        <strong>How to read this.</strong> Statements about other products come from their
        public documentation and positioning as of September 2026, not from running them
        side by side. Treat them as a starting point for your own evaluation, not as a
        benchmark. Where TermGuard is behind, it is listed plainly — that part is tested.
      </div>

      <h2 id="categories">The four kinds of tool this gets compared to</h2>

      <h3>1. Interactive style and terminology checkers</h3>
      <p>
        <em>Acrolinx, Congree, Writer, Vale (open source).</em> These work at authoring
        time: a sidebar in Word or an editor flags wording as it is written, and the author
        corrects it. Acrolinx is the established option in life sciences and is
        substantially broader than TermGuard — style, grammar, tone, readability, many
        languages, and integrations across the authoring stack.
      </p>
      <p>
        The difference is <strong>when</strong>. They are aimed at the document being
        written now. TermGuard is aimed at the documents that already exist and are already
        wrong — a device rename landing across several hundred files — and it produces the
        corrected copies rather than asking an author to make each edit.
      </p>
      <p className="sub">
        These are complementary, not alternatives. A programme that has one still has the
        other problem.
      </p>

      <h3>2. Regulated content platforms</h3>
      <p>
        <em>Veeva Vault (PromoMats, RIM, QualityDocs), MasterControl, ArisGlobal.</em>
        Systems of record: workflow, versioning, electronic signature and Part 11 audit
        trails, all far more mature than anything here. Veeva began shipping AI agents for
        promotional review in late 2025, with regulatory agents announced for 2026.
      </p>
      <p>
        <strong>These are not competitors.</strong> They hold and route documents; they do
        not do deep terminology remediation across an existing set. The sensible end state
        is TermGuard working on content that lives in one of them — which is why DMS
        integration is the first item on the roadmap rather than a nice-to-have.
      </p>

      <h3>3. AI regulatory writing</h3>
      <p>
        <em>Certara CoAuthor, Yseop.</em> These draft documents from source data. A
        different problem: authoring rather than harmonizing.
      </p>

      <h3>4. General-purpose AI redlining</h3>
      <p>
        <em>Claude for Word, Microsoft Copilot, contract-lifecycle tools.</em> These now
        write native tracked changes into Word, and they are the most direct comparison.
        The gap is not capability, it is evidence: no rulebook, no measured accuracy, no
        limit on what the model may touch, no per-change audit trail, and a document at a
        time rather than a corpus with a verification gate at the end.
      </p>

      <h2 id="behind">Where TermGuard is behind</h2>
      <p>
        This list is not a roadmap apology. It is the honest state of a demo, and the first
        two items are the ones that end conversations in a regulated environment.
      </p>
      <table>
        <thead>
          <tr><th>Capability</th><th>Established tools</th><th>TermGuard</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>Validation package</strong> (IQ/OQ/PQ)</td>
            <td>Shipped</td>
            <td><Badge kind="warn">None</Badge></td>
          </tr>
          <tr>
            <td><strong>Part 11 electronic signature</strong></td>
            <td>Shipped</td>
            <td><Badge kind="warn">Named participants, no authentication</Badge></td>
          </tr>
          <tr><td>Languages</td><td>Many; EU MDR makes this central</td><td>English only</td></tr>
          <tr><td>Checks</td><td>Style, grammar, tone, readability</td><td>Terminology only</td></tr>
          <tr><td>Formats</td><td>.docx, XML/DITA, PDF, CMS, web</td><td>.docx only</td></tr>
          <tr><td>Integrations</td><td>Word, Outlook, CMS, ticketing</td><td>A folder, or an API</td></tr>
          <tr><td>Authoring-time feedback</td><td>Yes</td><td>No — batch only</td></tr>
          <tr><td>Proven at scale</td><td>Thousands of documents</td><td>26, in a demo corpus</td></tr>
        </tbody>
      </table>

      <h2 id="ahead">Where it does something the others do not</h2>

      <h3>Retroactive remediation, in bulk, as tracked changes</h3>
      <p>
        The authoring-time checkers prevent new errors; the content platforms store
        documents. Neither takes a folder of existing files and returns redlined copies a
        reviewer can work through. That is the whole of what this does.
      </p>

      <h3>It can say how much the AI decided</h3>
      <p>
        Deterministic and AI-proposed changes are authored separately in Word, counted
        separately in every report, and separated in the audit export. When a machine
        records a decision on its own, it names the written policy clause that permitted
        it. In a market where most vendors are shipping AI features, being able to answer
        "how much of this did the model decide, and on whose authority" with a specific
        number is unusual.
      </p>

      <h3>It checks that it changed only what it said it changed</h3>
      <p>
        The verification gate rebuilds each document as approved, re-reads it, and fails if
        any text differs in a way no decision accounts for. Most tools can show you a
        successful run. This one is built to catch itself.
      </p>

      <h3>Its accuracy is measured, not asserted</h3>
      <p>
        The test suite generates a corpus with deliberately planted errors — including ones
        that must <em>not</em> be changed — and fails the build if recall, precision or the
        false-positive rate on clean documents slips. Those numbers are reproducible on any
        machine in about a minute.
      </p>

      <h2 id="questions">If you are evaluating this</h2>

      <h3>"We already use Acrolinx."</h3>
      <p>
        Keep it. It stops new documents going wrong at the point of writing. This corrects
        the ones already written, in bulk, with a record. If you have one, you still have
        the other problem.
      </p>

      <h3>"Why not just use Copilot or Claude in Word?"</h3>
      <p>
        For a single document, that may well be the right answer. The question to ask is
        what you hand an auditor after four hundred files have been edited that way: which
        rule justified each change, who approved it, and what proves nothing else moved.
      </p>

      <h3>"Is it validated?"</h3>
      <p>
        No. It is a working demonstration, not validated software, and validation is an
        engagement rather than a feature. What exists is the evidence a validation package
        would be built from: a measured test suite, an append-only audit trail, and a
        verification step that fails closed.
      </p>

      <h3>"Can we try it on our own documents?"</h3>
      <p>
        Yes, in report-only mode — it reads a folder, writes nothing, contacts no model,
        and returns the hit counts, where they sit, and an estimate of the review effort.
        It runs inside your own network. That is the right first step, before anything is
        changed.
      </p>
    </>
  );
}
