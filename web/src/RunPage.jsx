import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { BarChart, Empty, ErrorBanner, SplitBar, Stat } from "./components";

/**
 * The run screen: start a run, watch it progress, then read the summary.
 *
 * The two split bars are the point of this page. The first answers "how much of this
 * could be automated"; the second answers "how much did the AI decide" — the question a
 * regulatory reviewer asks first.
 */
export default function RunPage({ run, reload }) {
  const [running, setRunning] = useState(false);
  const [log, setLog] = useState([]);
  const [error, setError] = useState(null);
  const logRef = useRef(null);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  const start = useCallback(async () => {
    setRunning(true);
    setLog([]);
    setError(null);
    try {
      const { token } = await api.startRun();
      const source = new EventSource(api.urls.events(token));

      const append = (line) => setLog((previous) => [...previous, line]);
      const on = (name, format) =>
        source.addEventListener(name, (event) => append(format(JSON.parse(event.data))));

      on("run.started", (d) => `run ${d.run_id} started over ${d.files} files`);
      on("document.started", (d) => `[${d.index}/${d.of}] ${d.file}`);
      on("document.scanned", (d) => `    scanned ${d.file} — ${d.hits} hits`);
      on("document.redlined", (d) => `    redlined ${d.file} — ${d.changes} deterministic`);
      on("document.judged", (d) => `    judged ${d.file} — ${d.ai_changes} AI-proposed`);
      on("run.completed", (d) =>
        `done: ${d.deterministic} deterministic + ${d.ai} AI-proposed across ${d.files} files`);

      source.addEventListener("error", (event) => {
        try {
          setError(new Error(JSON.parse(event.data).message));
        } catch {
          /* transport-level error; the close handler reports it */
        }
      });
      source.addEventListener("done", async () => {
        source.close();
        setRunning(false);
        await reload();
      });
      source.onerror = () => {
        source.close();
        setRunning(false);
      };
    } catch (caught) {
      setError(caught);
      setRunning(false);
    }
  }, [reload]);

  const hits = run?.hits;
  const changes = run?.changes;
  const byClassification = hits?.by_classification || {};
  const byMechanism = changes?.by_mechanism || {};

  return (
    <>
      <h1>Run</h1>
      <p className="sub">
        Scan the corpus, write tracked changes, and route judgment calls to the reviewer.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card">
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <button className="primary" onClick={start} disabled={running}>
            {running ? "Running…" : "Start run"}
          </button>
          {run && (
            <span className="sub" style={{ margin: 0 }}>
              Showing run {run.run_id} · rulebook <code>{run.rulebook_hash}</code> · corpus{" "}
              <code>{run.corpus_hash}</code>
            </span>
          )}
        </div>
        {log.length > 0 && (
          <div className="log" ref={logRef} style={{ marginTop: 12 }}>
            {log.map((line, index) => (
              <div key={index}>{line}</div>
            ))}
          </div>
        )}
      </div>

      {!run ? (
        <Empty>No runs yet. Start one above.</Empty>
      ) : (
        <>
          <div className="row">
            <Stat label="Hits" value={hits?.total ?? 0} />
            <Stat label="Changes written" value={changes?.total ?? 0} />
            <Stat
              label="Decided"
              value={changes?.decided ?? 0}
              hint={`${changes?.pending ?? 0} still pending`}
            />
            <Stat label="Files" value={run.files?.length ?? 0} />
          </div>

          <div className="card">
            <SplitBar
              title="What the scanner found"
              left={byClassification.unambiguous || 0}
              right={byClassification.needs_judgment || 0}
              leftLabel="Unambiguous"
              rightLabel="Needs judgment"
              leftColor="var(--rule)"
              rightColor="var(--warn)"
            />
            <SplitBar
              title="Who made each change"
              left={byMechanism.deterministic || 0}
              right={byMechanism.ai || 0}
              leftLabel="Rule engine"
              rightLabel="AI-proposed"
            />
          </div>

          <div className="row">
            <div className="card">
              <h3>Hits by document part</h3>
              <BarChart data={hits?.by_part} />
              <p className="sub" style={{ marginTop: 10, marginBottom: 0 }}>
                {Object.entries(hits?.by_part || {})
                  .filter(([part]) => part !== "body")
                  .reduce((sum, [, count]) => sum + count, 0)}{" "}
                hits sit outside the body — headers, footers, footnotes. These are the ones
                a manual pass misses.
              </p>
            </div>
            <div className="card">
              <h3>Hits by rule</h3>
              <BarChart data={hits?.by_rule} />
            </div>
          </div>

          <div className="card">
            <h3>Files</h3>
            <table>
              <thead>
                <tr>
                  <th>File</th>
                  <th>Type</th>
                  <th className="num">Hits</th>
                  <th className="num">Rule engine</th>
                  <th className="num">AI</th>
                  <th>Download</th>
                </tr>
              </thead>
              <tbody>
                {run.files.map((file) => (
                  <tr key={file.document_id}>
                    <td>{file.name}</td>
                    <td>{file.doc_type || "—"}</td>
                    <td className="num">{file.hits}</td>
                    <td className="num">{file.mechanisms?.deterministic || 0}</td>
                    <td className="num">{file.mechanisms?.ai || 0}</td>
                    <td>
                      {file.hits > 0 ? (
                        <a href={api.urls.redlined(run.run_id, file.name)}>redlined</a>
                      ) : (
                        <span className="sub">no changes</span>
                      )}
                    </td>
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
