import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Badge, Empty, ErrorBanner } from "./components";

const SUPPORTED = ".docx,.pdf,.txt,.md,.vtt,.srt,.png,.jpg,.jpeg,.webp,.mp4,.mov,.m4a,.mp3";

/**
 * Upload a style guide, glossary, screenshot or recording; review what it yields.
 *
 * The quote is the largest thing on each candidate, deliberately. The reviewer's job is
 * not to judge a proposed rule in the abstract — it is to read the sentence and decide
 * whether the rule reads it correctly. A layout that leads with the rule invites people
 * to approve plausible-looking pairs without checking them against anything.
 */
export default function SourcesPage() {
  const [sources, setSources] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [stale, setStale] = useState([]);
  const [url, setUrl] = useState("");
  const [reviewer, setReviewer] = useState(
    () => localStorage.getItem("termguard.reviewer") || "reviewer@meridian"
  );
  const [editing, setEditing] = useState(null);   // candidate_id being corrected
  const [draft, setDraft] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("proposed");
  const fileInput = useRef(null);

  const load = useCallback(async () => {
    try {
      const [s, c, r] = await Promise.all([
        api.sources(),
        api.candidates({ status: filter }),
        api.staleRuns().catch(() => []),
      ]);
      setSources(s);
      setCandidates(c.items);
      setStale(r);
      setError(null);
    } catch (caught) {
      setError(caught);
    }
  }, [filter]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };

  const onFiles = (files) =>
    act(async () => {
      for (const file of files) await api.uploadSource(file, reviewer);
    });

  return (
    <>
      <h1>Sources</h1>
      <p className="sub">
        Upload a style guide, glossary, screenshot or recording — or point at a web page.
        Terminology found in it becomes candidate rules for you to review.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {stale.length > 0 && (
        <div className="attention warn">
          <span className="glyph">!</span>
          <div>
            <strong>
              {stale.length} run{stale.length === 1 ? "" : "s"} predate the current rulebook
            </strong>
            <div className="sub" style={{ margin: 0 }}>
              Run{stale.length === 1 ? " " : "s "}
              {stale.map((r) => r.run_id).join(", ")} ran under{" "}
              <code>{stale[0].ran_under}</code>; the rulebook is now{" "}
              <code>{stale[0].current}</code>. Verification will not re-verify them — start
              a new run to apply the rules you have added.
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <div
          className="dropzone"
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            if (e.dataTransfer.files?.length) onFiles([...e.dataTransfer.files]);
          }}
        >
          <strong>Drop files here, or click to choose</strong>
          <div className="sub" style={{ margin: "4px 0 0" }}>
            Word, PDF, text · images · video with captions (.vtt / .srt)
          </div>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept={SUPPORTED}
            style={{ display: "none" }}
            onChange={(e) => e.target.files?.length && onFiles([...e.target.files])}
          />
        </div>

        <div style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "center" }}>
          <input
            type="text"
            placeholder="https://example.com/terminology-standard"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button
            disabled={busy || !url.trim()}
            onClick={() =>
              act(async () => {
                await api.fetchSourceUrl({ url: url.trim(), uploaded_by: reviewer });
                setUrl("");
              })
            }
          >
            Fetch page
          </button>
          <input
            type="text"
            value={reviewer}
            onChange={(e) => {
              setReviewer(e.target.value);
              localStorage.setItem("termguard.reviewer", e.target.value);
            }}
            style={{ maxWidth: 210 }}
            aria-label="Your name"
          />
        </div>
      </div>

      {sources.length > 0 && (
        <div className="card">
          <h3>Sources taken in</h3>
          <table>
            <thead>
              <tr>
                <th>Source</th><th>Type</th><th className="num">Lines</th>
                <th className="num">Candidates</th><th>Status</th><th>Added by</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((source) => (
                <tr key={source.source_id}>
                  <td title={source.origin}>{source.name}</td>
                  <td><Badge kind="plain">{source.kind}</Badge></td>
                  <td className="num">{source.lines_read || "—"}</td>
                  <td className="num">{source.candidates_found || "—"}</td>
                  <td>
                    {source.readable ? (
                      <span className="sub">
                        {Object.entries(source.by_status)
                          .map(([k, v]) => `${v} ${k}`)
                          .join(", ") || "read"}
                      </span>
                    ) : (
                      <Badge kind="warn">could not read</Badge>
                    )}
                  </td>
                  <td className="sub">{source.uploaded_by}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {sources.filter((s) => s.needs?.length).map((source) => (
            <div className="help-note" key={`needs-${source.source_id}`}>
              <strong>{source.name}</strong> — {source.needs.join("; ")}
            </div>
          ))}
        </div>
      )}

      <h2>
        Candidate rules
        <span className="sub" style={{ fontWeight: 400, marginLeft: 10, fontSize: 13 }}>
          nothing here is in force until you accept it
        </span>
      </h2>

      <div className="card" style={{ display: "flex", gap: 8 }}>
        {["proposed", "accepted", "rejected", "all"].map((option) => (
          <button
            key={option}
            className={filter === option ? "primary" : ""}
            onClick={() => setFilter(option)}
          >
            {option}
          </button>
        ))}
      </div>

      {candidates.length === 0 ? (
        <Empty>
          {filter === "proposed"
            ? "Nothing awaiting review. Upload a source above."
            : `No ${filter} candidates.`}
        </Empty>
      ) : (
        candidates.map((candidate) => {
          const conflicted = candidate.warnings.some((w) => w.startsWith("CONFLICT"));
          const isEditing = editing === candidate.candidate_id;
          return (
            <div className="card candidate" key={candidate.candidate_id}>
              {/* The sentence first: this is what is actually being reviewed. */}
              <blockquote className="quote">
                {candidate.quote || <em>no quote captured</em>}
                <cite>
                  {candidate.source} · {candidate.locator}
                </cite>
              </blockquote>

              <div className="candidate-rule">
                {isEditing ? (
                  <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <input
                      type="text"
                      value={draft.deprecated ?? candidate.deprecated}
                      onChange={(e) => setDraft({ ...draft, deprecated: e.target.value })}
                      style={{ maxWidth: 220 }}
                    />
                    <span className="sub">→</span>
                    <input
                      type="text"
                      value={draft.approved ?? candidate.approved}
                      onChange={(e) => setDraft({ ...draft, approved: e.target.value })}
                      style={{ maxWidth: 220 }}
                    />
                  </span>
                ) : (
                  <>
                    <span className="del">{candidate.deprecated}</span>
                    <span className="sub" style={{ margin: "0 8px" }}>→</span>
                    <span className="ins">{candidate.approved}</span>
                  </>
                )}
                <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                  <Badge kind="plain">{candidate.method}</Badge>
                  <Badge kind={candidate.confidence >= 0.9 ? "ok" : "plain"}>
                    {Math.round(candidate.confidence * 100)}%
                  </Badge>
                  {candidate.suggested?.context_required && (
                    <Badge kind="warn">needs judgment</Badge>
                  )}
                </span>
              </div>

              {candidate.note && (
                <p className="sub" style={{ margin: "8px 0 0" }}>{candidate.note}</p>
              )}

              {candidate.warnings.map((warning, index) => (
                <div
                  className={`attention ${warning.startsWith("CONFLICT") ? "error" : "info"}`}
                  key={index}
                  style={{ marginTop: 8 }}
                >
                  <span className="glyph">{warning.startsWith("CONFLICT") ? "!!" : "i"}</span>
                  <div>{warning}</div>
                </div>
              ))}

              {candidate.status === "proposed" ? (
                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  <button
                    className="accept"
                    disabled={busy}
                    onClick={() =>
                      act(async () => {
                        await api.acceptCandidate(candidate.candidate_id, {
                          reviewer,
                          overrides: isEditing ? draft : null,
                        });
                        setEditing(null);
                        setDraft({});
                      })
                    }
                  >
                    {isEditing ? "Save and add" : conflicted ? "Add anyway" : "Add rule"}
                  </button>
                  <button
                    className="reject"
                    disabled={busy}
                    onClick={() =>
                      act(() =>
                        api.rejectCandidate(candidate.candidate_id, { reviewer })
                      )
                    }
                  >
                    Reject
                  </button>
                  {!isEditing && (
                    <button
                      disabled={busy}
                      onClick={() => {
                        setEditing(candidate.candidate_id);
                        setDraft({});
                      }}
                    >
                      Correct it
                    </button>
                  )}
                  {isEditing && (
                    <button onClick={() => { setEditing(null); setDraft({}); }}>
                      Cancel
                    </button>
                  )}
                </div>
              ) : (
                <p className="sub" style={{ margin: "10px 0 0" }}>
                  {candidate.status}
                  {candidate.rule_id ? ` as ${candidate.rule_id}` : ""}
                  {candidate.decided_by ? ` by ${candidate.decided_by}` : ""}
                </p>
              )}
            </div>
          );
        })
      )}
    </>
  );
}
