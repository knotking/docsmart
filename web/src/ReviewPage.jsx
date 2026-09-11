import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { Empty, ErrorBanner, InlineDiff, MechanismBadge } from "./components";

/**
 * The reviewer queue — the screen that actually gets used.
 *
 * Keyboard first: j/k move, a accepts, r rejects, e opens the edit field. The list
 * advances on decision so a reviewer can clear a queue without touching the mouse.
 */
export default function ReviewPage({ run, reload }) {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(0);
  const [filters, setFilters] = useState({ file: "", rule: "", mechanism: "" });
  const [editing, setEditing] = useState(null);
  const [reviewer, setReviewer] = useState(
    () => localStorage.getItem("termguard.reviewer") || "reviewer@meridian"
  );
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!run) return;
    try {
      const body = await api.queue(run.run_id, { ...filters, status: "pending" });
      setItems(body.items);
      setSelected((current) => Math.min(current, Math.max(0, body.items.length - 1)));
    } catch (caught) {
      setError(caught);
    }
  }, [run, filters]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    localStorage.setItem("termguard.reviewer", reviewer);
  }, [reviewer]);

  const current = items[selected];

  const decide = useCallback(
    async (decision, finalText) => {
      if (!current || busy) return;
      setBusy(true);
      try {
        await api.decide(current.change_id, {
          decision,
          reviewer,
          final_text: finalText ?? null,
        });
        // Drop the decided item and keep the cursor where it was, so the next item
        // slides under it — that is what makes clearing a queue fast.
        setItems((previous) => {
          const next = previous.filter((item) => item.change_id !== current.change_id);
          setSelected((index) => Math.min(index, Math.max(0, next.length - 1)));
          return next;
        });
        setEditing(null);
        reload();
      } catch (caught) {
        setError(caught);
      } finally {
        setBusy(false);
      }
    },
    [current, reviewer, busy, reload]
  );

  useEffect(() => {
    const onKey = (event) => {
      if (event.target.matches("input, textarea, select")) return;
      if (event.key === "j") setSelected((i) => Math.min(i + 1, items.length - 1));
      else if (event.key === "k") setSelected((i) => Math.max(i - 1, 0));
      else if (event.key === "a") decide("accepted");
      else if (event.key === "r") decide("rejected");
      else if (event.key === "e" && current) {
        event.preventDefault();
        setEditing(proposedSentence(current));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items.length, decide, current]);

  const fileOptions = useMemo(
    () => (run?.files || []).map((file) => file.name),
    [run]
  );
  const ruleOptions = useMemo(
    () => Object.keys(run?.hits?.by_rule || {}),
    [run]
  );

  if (!run) return <Empty>No run to review yet.</Empty>;

  return (
    <>
      <h1>Review</h1>
      <p className="sub">
        {items.length} change{items.length === 1 ? "" : "s"} awaiting a decision.
        Every ruling is recorded against your name and never overwrites an earlier one.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <select
          value={filters.file}
          onChange={(event) => setFilters({ ...filters, file: event.target.value })}
        >
          <option value="">All files</option>
          {fileOptions.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
        </select>
        <select
          value={filters.rule}
          onChange={(event) => setFilters({ ...filters, rule: event.target.value })}
        >
          <option value="">All rules</option>
          {ruleOptions.map((id) => (
            <option key={id} value={id}>{id}</option>
          ))}
        </select>
        <select
          value={filters.mechanism}
          onChange={(event) => setFilters({ ...filters, mechanism: event.target.value })}
        >
          <option value="">Both mechanisms</option>
          <option value="deterministic">Rule engine</option>
          <option value="ai">AI-proposed</option>
        </select>
        <input
          type="text"
          value={reviewer}
          onChange={(event) => setReviewer(event.target.value)}
          style={{ maxWidth: 220 }}
          aria-label="Reviewer name"
        />
      </div>

      {items.length === 0 ? (
        <Empty>Queue clear. Every change has a decision — run verification next.</Empty>
      ) : (
        <div className="review">
          <div className="queue">
            {items.map((item, index) => (
              <div
                key={item.change_id}
                className={`queue-item ${index === selected ? "selected" : ""}`}
                onClick={() => setSelected(index)}
              >
                <div className="file">
                  {item.file} · {item.part}
                </div>
                <div className="term">
                  {item.original_text}
                  {item.proposed_text ? ` → ${item.proposed_text}` : " (keep?)"}
                </div>
                <div style={{ marginTop: 3 }}>
                  <MechanismBadge mechanism={item.mechanism} />{" "}
                  <span className="mono" style={{ color: "var(--muted)" }}>{item.rule_id}</span>
                </div>
              </div>
            ))}
          </div>

          <div className="card">
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
              <div>
                <strong>{current.file}</strong>{" "}
                <span className="sub" style={{ margin: 0 }}>
                  {current.location || current.part} · paragraph {current.paragraph_index}
                </span>
              </div>
              <MechanismBadge mechanism={current.mechanism} model={current.model} />
            </div>

            <h3>Proposed</h3>
            <div className="diff">
              <InlineDiff
                text={current.sentence || current.paragraph_text}
                original={current.original_text}
                proposed={current.proposed_text}
              />
            </div>

            {current.paragraph_text && current.paragraph_text !== current.sentence && (
              <>
                <h3 style={{ marginTop: 16 }}>Paragraph context</h3>
                <p className="sub">{current.paragraph_text}</p>
              </>
            )}

            <h3 style={{ marginTop: 16 }}>Rule</h3>
            <dl className="kv">
              <dt>Rule</dt>
              <dd>
                <code>{current.rule_id}</code> — {current.classification}
              </dd>
              <dt>Why it is here</dt>
              <dd>{current.reason || "—"}</dd>
              {current.mechanism === "ai" && (
                <>
                  <dt>Model</dt>
                  <dd className="mono">{current.model}</dd>
                  <dt>Prompt hash</dt>
                  <dd className="mono">{current.prompt_hash}</dd>
                  <dt>Model decision</dt>
                  <dd>{current.llm_decision}</dd>
                  <dt>Justification</dt>
                  <dd>{current.justification}</dd>
                </>
              )}
              <dt>Comment in Word</dt>
              <dd className="sub" style={{ margin: 0 }}>{current.comment}</dd>
            </dl>

            {editing !== null && (
              <div style={{ marginTop: 14 }}>
                <h3>Your wording</h3>
                <textarea
                  rows={3}
                  value={editing}
                  onChange={(event) => setEditing(event.target.value)}
                  autoFocus
                />
              </div>
            )}

            <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
              {editing === null ? (
                <>
                  <button className="accept" onClick={() => decide("accepted")} disabled={busy}>
                    Accept
                  </button>
                  <button className="reject" onClick={() => decide("rejected")} disabled={busy}>
                    Reject
                  </button>
                  <button onClick={() => setEditing(proposedSentence(current))} disabled={busy}>
                    Edit
                  </button>
                </>
              ) : (
                <>
                  <button
                    className="primary"
                    onClick={() => decide("edited", editing)}
                    disabled={busy || !editing.trim()}
                  >
                    Save edit
                  </button>
                  <button onClick={() => setEditing(null)}>Cancel</button>
                </>
              )}
            </div>

            <p className="keys">
              <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>a</kbd> accept · <kbd>r</kbd> reject ·{" "}
              <kbd>e</kbd> edit
            </p>
          </div>
        </div>
      )}
    </>
  );
}

/** The sentence as it would read if the change were accepted — what Edit pre-fills with. */
function proposedSentence(item) {
  const sentence = item.sentence || item.paragraph_text || "";
  if (!item.original_text || !item.proposed_text) return sentence;
  return sentence.replace(item.original_text, item.proposed_text);
}
