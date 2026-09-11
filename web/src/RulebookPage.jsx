import { useEffect, useState } from "react";
import { api } from "./api";
import { Badge, Empty, ErrorBanner } from "./components";

/**
 * The rulebook. Editable in place, because the rulebook is the client's asset and the
 * thing they will want to change first. Saving re-validates server-side and bumps the
 * hash, which every later report is stamped with.
 */
export default function RulebookPage() {
  const [book, setBook] = useState(null);
  const [rules, setRules] = useState([]);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(null);

  useEffect(() => {
    api.rulebook().then((body) => {
      setBook(body);
      setRules(body.rules);
    }).catch(setError);
  }, []);

  const update = (index, field, value) => {
    setRules((previous) =>
      previous.map((rule, i) => (i === index ? { ...rule, [field]: value } : rule))
    );
    setDirty(true);
    setSaved(null);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const result = await api.saveRulebook({ rules, version: book.version });
      setSaved(result);
      setDirty(false);
      const refreshed = await api.rulebook();
      setBook(refreshed);
      setRules(refreshed.rules);
    } catch (caught) {
      setError(caught);
    } finally {
      setSaving(false);
    }
  };

  if (!book) return <Empty>Loading rulebook…</Empty>;

  return (
    <>
      <h1>Rulebook</h1>
      <p className="sub">
        {rules.length} rules · hash <code>{book.hash}</code> · last modified{" "}
        {book.modified_at ? new Date(book.modified_at).toLocaleString() : "unknown"}
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {saved && (
        <div className="card" style={{ borderColor: "var(--ok)" }}>
          {saved.changed ? (
            <>Saved. Hash moved from <code>{saved.previous_hash}</code> to <code>{saved.hash}</code>.
            Re-run the pipeline to apply it.</>
          ) : (
            <>Saved — no rule content changed, so the hash is unchanged.</>
          )}
        </div>
      )}

      <div className="card" style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <button className="primary" onClick={save} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save rulebook"}
        </button>
        {dirty && <span className="sub" style={{ margin: 0 }}>Unsaved changes.</span>}
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th>Deprecated</th>
              <th>Approved</th>
              <th>Context</th>
              <th>Exceptions</th>
              <th>Scope</th>
            </tr>
          </thead>
          <tbody>
            {rules.map((rule, index) => (
              <tr key={rule.id}>
                <td>
                  <code>{rule.id}</code>
                  <div className="sub" style={{ margin: 0 }}>{rule.match} · {rule.case}</div>
                </td>
                <td className="mono">{rule.deprecated.join(", ")}</td>
                <td>
                  <input
                    type="text"
                    value={rule.approved}
                    onChange={(event) => update(index, "approved", event.target.value)}
                  />
                </td>
                <td style={{ width: 90 }}>
                  <label style={{ display: "flex", gap: 5, alignItems: "center" }}>
                    <input
                      type="checkbox"
                      checked={rule.context_required}
                      onChange={(event) => update(index, "context_required", event.target.checked)}
                    />
                    {rule.context_required && <Badge kind="warn">judge</Badge>}
                  </label>
                </td>
                <td>
                  <input
                    type="text"
                    value={(rule.exceptions || []).join(" | ")}
                    placeholder="none"
                    onChange={(event) =>
                      update(
                        index,
                        "exceptions",
                        event.target.value.split("|").map((s) => s.trim()).filter(Boolean)
                      )
                    }
                  />
                </td>
                <td className="sub">{(rule.scope || []).length} parts</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
