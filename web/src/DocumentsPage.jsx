import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { Badge, Empty, ErrorBanner, MechanismBadge } from "./components";

/**
 * The lifecycle screen: every document, every version it has ever had, and the events
 * between them. This is what answers "what did this file look like then, what changed,
 * who or what changed it, and can you prove the archive is intact".
 */
export default function DocumentsPage() {
  const [documents, setDocuments] = useState([]);
  const [selected, setSelected] = useState(null);
  const [versions, setVersions] = useState([]);
  const [timeline, setTimeline] = useState([]);
  const [integrity, setIntegrity] = useState(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.documents()
      .then((body) => {
        setDocuments(body);
        if (body.length) setSelected(body[0].document_id);
      })
      .catch(setError);
  }, []);

  useEffect(() => {
    if (selected == null) return;
    Promise.all([api.versions(selected), api.timeline(selected)])
      .then(([v, t]) => {
        setVersions(v);
        setTimeline(t);
      })
      .catch(setError);
  }, [selected]);

  const checkIntegrity = useCallback(async () => {
    setChecking(true);
    try {
      setIntegrity(await api.integrity());
    } catch (caught) {
      setError(caught);
    } finally {
      setChecking(false);
    }
  }, []);

  if (!documents.length) {
    return <Empty>No documents ingested yet. Start a run first.</Empty>;
  }

  const current = documents.find((d) => d.document_id === selected);

  return (
    <>
      <h1>Documents</h1>
      <p className="sub">
        Every document under management, with its full version chain. Any past version can
        be downloaded exactly as it was.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <button onClick={checkIntegrity} disabled={checking}>
          {checking ? "Checking…" : "Verify archive integrity"}
        </button>
        {integrity && (
          <span>
            {integrity.ok ? (
              <Badge kind="ok">
                {integrity.checked} versions re-hashed, all intact
              </Badge>
            ) : (
              <Badge kind="warn">
                {integrity.failures.length} of {integrity.checked} versions failed
              </Badge>
            )}
          </span>
        )}
        <span className="sub" style={{ margin: 0, marginLeft: "auto" }}>
          {documents.length} documents ·{" "}
          {documents.reduce((sum, d) => sum + d.versions, 0)} versions
        </span>
      </div>

      <div className="review">
        <div className="queue">
          {documents.map((document) => (
            <div
              key={document.document_id}
              className={`queue-item ${document.document_id === selected ? "selected" : ""}`}
              onClick={() => setSelected(document.document_id)}
            >
              <div className="term">{document.name}</div>
              <div className="file">
                {document.versions} version{document.versions === 1 ? "" : "s"} ·{" "}
                {document.current_stage}
              </div>
            </div>
          ))}
        </div>

        <div>
          <div className="card">
            <h3>Version chain — {current?.name}</h3>
            <table>
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Stage</th>
                  <th>Content SHA-256</th>
                  <th className="num">Size</th>
                  <th className="num">Changes</th>
                  <th>Produced by</th>
                  <th>Download</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((version) => (
                  <tr key={version.version_id}>
                    <td>v{version.version_no}</td>
                    <td><Badge kind="plain">{version.stage}</Badge></td>
                    <td className="mono" title={version.sha256}>
                      {version.sha256.slice(0, 16)}…
                    </td>
                    <td className="num">{(version.size_bytes / 1024).toFixed(1)} KB</td>
                    <td className="num">{version.changes || "—"}</td>
                    <td className="sub">{version.actor}</td>
                    <td>
                      <a href={api.urls.version(selected, version.version_no)}>.docx</a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {versions.some((v) => v.summary && Object.keys(v.summary).length) && (
              <p className="sub" style={{ marginTop: 12, marginBottom: 0 }}>
                {versions
                  .filter((v) => v.summary?.deterministic != null)
                  .map((v) => (
                    <span key={v.version_id}>
                      v{v.version_no}: {v.summary.deterministic} rule-engine,{" "}
                      {v.summary.ai} AI-proposed
                      {v.summary.ai_kept ? `, ${v.summary.ai_kept} kept` : ""}
                    </span>
                  ))}
              </p>
            )}
          </div>

          <div className="card">
            <h3>Timeline</h3>
            <div className="timeline">
              {timeline.map((row, index) => (
                <div
                  key={index}
                  className={`tl-item ${row.kind === "version" ? "version" : ""}`}
                >
                  <div className="when">{new Date(row.at).toLocaleString()}</div>
                  {row.kind === "version" ? (
                    <div>
                      <strong>
                        v{row.version_no} — {row.stage}
                      </strong>{" "}
                      <span className="mono sub">{row.short_sha}</span>
                      <div className="sub" style={{ margin: 0 }}>
                        {row.actor} {row.note ? `· ${row.note}` : ""}
                      </div>
                    </div>
                  ) : (
                    <div>
                      <code>{row.event}</code>
                      {row.mechanism && (
                        <>
                          {" "}
                          <MechanismBadge mechanism={row.mechanism} />
                        </>
                      )}
                      <div className="sub" style={{ margin: 0 }}>{row.summary}</div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
