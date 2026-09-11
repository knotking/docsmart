// Shared presentation pieces. The mechanism badge and the two-segment bar appear on
// every screen a change is visible on, so they live here and stay identical.

export function Badge({ kind, children }) {
  return <span className={`badge ${kind}`}>{children}</span>;
}

export function MechanismBadge({ mechanism, model }) {
  if (mechanism === "ai") {
    return <Badge kind="ai">AI-proposed{model ? ` · ${model}` : ""}</Badge>;
  }
  return <Badge kind="rule">Rule engine</Badge>;
}

/** The chart that answers "how much did the AI decide". */
export function SplitBar({ title, left, right, leftLabel, rightLabel, leftColor, rightColor }) {
  const total = left + right;
  const pct = (value) => (total === 0 ? 0 : (value / total) * 100);
  return (
    <div className="split">
      <div className="split-head">
        <strong>{title}</strong>
        <span>{total.toLocaleString()} total</span>
      </div>
      <div className="split-bar">
        <div
          className="split-seg"
          style={{ width: `${pct(left)}%`, background: leftColor || "var(--rule)" }}
          title={`${leftLabel}: ${left}`}
        >
          {pct(left) > 9 ? left : ""}
        </div>
        <div
          className="split-seg"
          style={{ width: `${pct(right)}%`, background: rightColor || "var(--ai)" }}
          title={`${rightLabel}: ${right}`}
        >
          {pct(right) > 9 ? right : ""}
        </div>
      </div>
      <div className="split-legend">
        <span>
          <i className="swatch" style={{ background: leftColor || "var(--rule)" }} />
          {leftLabel} — {left.toLocaleString()}
        </span>
        <span>
          <i className="swatch" style={{ background: rightColor || "var(--ai)" }} />
          {rightLabel} — {right.toLocaleString()}
        </span>
      </div>
    </div>
  );
}

export function BarChart({ data, max, color }) {
  const entries = Object.entries(data || {});
  if (!entries.length) return <p className="empty">Nothing to show.</p>;
  const ceiling = max || Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div>
      {entries.map(([label, value]) => (
        <div className="hbar" key={label}>
          <span>{label}</span>
          <div className="track">
            <div
              className="fill"
              style={{ width: `${(value / ceiling) * 100}%`, background: color }}
            />
          </div>
          <span className="count">{value}</span>
        </div>
      ))}
    </div>
  );
}

export function Stat({ label, value, hint }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className="stat">{typeof value === "number" ? value.toLocaleString() : value}</div>
      {hint && <div className="sub" style={{ margin: 0 }}>{hint}</div>}
    </div>
  );
}

/** A sentence with the deletion struck through and the insertion highlighted. */
export function InlineDiff({ text, original, proposed }) {
  if (!text || !original) return <span>{text}</span>;
  const index = text.indexOf(original);
  if (index < 0) {
    return (
      <span>
        {text} <span className="ins">{proposed}</span>
      </span>
    );
  }
  return (
    <span>
      {text.slice(0, index)}
      <span className="del">{original}</span>
      {proposed ? <span className="ins">{proposed}</span> : null}
      {text.slice(index + original.length)}
    </span>
  );
}

export function ErrorBanner({ error, onDismiss }) {
  if (!error) return null;
  return (
    <div className="error" onClick={onDismiss} role="alert">
      {String(error.message || error)}
    </div>
  );
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}
