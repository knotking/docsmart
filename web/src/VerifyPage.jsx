import { useCallback, useState } from "react";
import { api } from "./api";
import { Badge, Empty, ErrorBanner, SplitBar, Stat } from "./components";

/**
 * The verification gate. Running it is the last step of the demo: it rebuilds the
 * as-accepted corpus, re-scans it, and refuses to pass while anything is unproven.
 */
export default function VerifyPage({ run }) {
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const verify = useCallback(async () => {
    if (!run) return;
    setBusy(true);
    setError(null);
    try {
      setReport(await api.verify(run.run_id));
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, [run]);

  if (!run) return <Empty>No run to verify yet.</Empty>;

  const totals = report?.totals;

  return (
    <>
      <h1>Verify</h1>
      <p className="sub">
        Applies every reviewer decision, re-scans the result with the same rulebook, and
        proves nothing else was touched.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <button className="primary" onClick={verify} disabled={busy}>
          {busy ? "Verifying…" : "Run verification"}
        </button>
        <a href={api.urls.auditCsv(run.run_id)}>
          <button>Download audit CSV</button>
        </a>
        {report && (
          <span style={{ marginLeft: "auto" }}>
            {report.passed ? <Badge kind="ok">PASS</Badge> : <Badge kind="warn">FAIL</Badge>}
          </span>
        )}
      </div>

      {!report ? (
        <Empty>Run verification to see the verdict.</Empty>
      ) : (
        <>
          <div className="row">
            <Stat label="Files clean" value={`${totals.passed} / ${totals.files}`} />
            <Stat label="Unadjudicated violations" value={totals.remaining_hits} />
            <Stat label="Undecided changes" value={totals.undecided} />
            <Stat label="Unexplained edits" value={totals.unexplained_edits} />
          </div>

          <div className="card">
            <SplitBar
              title="Applied changes by mechanism"
              left={totals.deterministic}
              right={totals.ai}
              leftLabel="Rule engine"
              rightLabel="AI-proposed"
            />
            <dl className="kv" style={{ marginTop: 14 }}>
              <dt>Rulebook hash</dt>
              <dd><code>{report.rulebook_hash}</code></dd>
              <dt>Corpus hash</dt>
              <dd><code>{report.corpus_hash}</code></dd>
              <dt>Reviewers</dt>
              <dd>{report.reviewers.join(", ") || "none recorded"}</dd>
              <dt>Verified at</dt>
              <dd className="mono">{report.verified_at}</dd>
            </dl>
          </div>

          <div className="card">
            <h3>Gate checks</h3>
            <table>
              <tbody>
                <Check
                  label="Every change decided"
                  ok={totals.undecided === 0}
                  detail={`${totals.undecided} undecided`}
                />
                <Check
                  label="Every remaining term adjudicated"
                  ok={totals.remaining_hits === 0}
                  detail={`${totals.remaining_hits} unadjudicated`}
                />
                <Check
                  label="No disputed keeps outstanding"
                  ok={totals.disputed === 0}
                  detail={`${totals.disputed} disputed`}
                />
                <Check
                  label="No unexplained edits"
                  ok={totals.unexplained_edits === 0}
                  detail={`${totals.unexplained_edits} found`}
                />
              </tbody>
            </table>
          </div>

          {totals.adjudicated_exceptions > 0 && (
            <div className="card">
              <h3>Ratified exceptions ({totals.adjudicated_exceptions})</h3>
              <p className="sub">
                Deprecated terms that remain because a reviewer decided to keep them —
                patient-facing plain language, quoted regulatory text. Each is a recorded
                decision, not an oversight.
              </p>
              <table>
                <thead>
                  <tr><th>File</th><th>Rule</th><th>Term</th><th>Location</th></tr>
                </thead>
                <tbody>
                  {report.files.flatMap((file) =>
                    file.adjudicated_exceptions.map((entry, index) => (
                      <tr key={`${file.file}-${index}`}>
                        <td>{file.file}</td>
                        <td><code>{entry.rule_id}</code></td>
                        <td>{entry.matched}</td>
                        <td className="sub">{entry.location}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          )}

          <div className="card">
            <h3>Files</h3>
            <table>
              <thead>
                <tr>
                  <th>File</th>
                  <th>Result</th>
                  <th className="num">Accepted</th>
                  <th className="num">Edited</th>
                  <th className="num">Rejected</th>
                  <th className="num">Kept</th>
                  <th>Final SHA-256</th>
                  <th>Download</th>
                </tr>
              </thead>
              <tbody>
                {report.files.map((file) => (
                  <tr key={file.file}>
                    <td>{file.file}</td>
                    <td>
                      {file.passed ? (
                        <Badge kind="ok">clean</Badge>
                      ) : (
                        <Badge kind="warn">{file.reasons.join("; ")}</Badge>
                      )}
                    </td>
                    <td className="num">{file.accepted}</td>
                    <td className="num">{file.edited}</td>
                    <td className="num">{file.rejected}</td>
                    <td className="num">{file.kept}</td>
                    <td className="mono">{(file.final_sha256 || "").slice(0, 12)}</td>
                    <td><a href={api.urls.final(run.run_id, file.file)}>final</a></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

function Check({ label, ok, detail }) {
  return (
    <tr>
      <td>{label}</td>
      <td style={{ width: 90 }}>
        {ok ? <Badge kind="ok">PASS</Badge> : <Badge kind="warn">FAIL</Badge>}
      </td>
      <td className="sub">{detail}</td>
    </tr>
  );
}
