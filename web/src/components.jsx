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

/**
 * The chart that answers "how much did the AI decide".
 *
 * A two-segment proportion bar. The 2px surface gap between segments is deliberate:
 * abutting fills of similar lightness blur into one mark, and this distinction is the
 * one a regulatory reader is looking for. The legend is always present and both segments
 * carry their number, so identity never rests on colour alone.
 */
export function SplitBar({ title, left, right, leftLabel, rightLabel, leftColor, rightColor }) {
  const total = left + right;
  const pct = (value) => (total === 0 ? 0 : (value / total) * 100);
  const share = (value) => (total === 0 ? "" : `${Math.round((value / total) * 100)}%`);
  const l = leftColor || "var(--rule)";
  const r = rightColor || "var(--ai)";

  return (
    <div className="split">
      <div className="split-head">
        <strong>{title}</strong>
        <span>{total.toLocaleString()} total</span>
      </div>
      <div className="split-bar">
        <div
          className="split-seg"
          style={{ width: `${pct(left)}%`, background: l }}
          title={`${leftLabel}: ${left.toLocaleString()} (${share(left)})`}
        >
          {pct(left) > 12 ? left.toLocaleString() : ""}
        </div>
        {left > 0 && right > 0 && <div className="split-gap" />}
        <div
          className="split-seg"
          style={{ width: `${pct(right)}%`, background: r }}
          title={`${rightLabel}: ${right.toLocaleString()} (${share(right)})`}
        >
          {pct(right) > 12 ? right.toLocaleString() : ""}
        </div>
      </div>
      <div className="split-legend">
        <span>
          <i className="swatch" style={{ background: l }} />
          {leftLabel} — {left.toLocaleString()} {total > 0 && `(${share(left)})`}
        </span>
        <span>
          <i className="swatch" style={{ background: r }} />
          {rightLabel} — {right.toLocaleString()} {total > 0 && `(${share(right)})`}
        </span>
      </div>
    </div>
  );
}

/**
 * A single proportion with its own label — an acceptance rate, an automation rate.
 *
 * Renders "no data" rather than 0% when the denominator is empty. Those mean completely
 * different things, and a dashboard that draws them identically will mislead somebody
 * into acting on an absence.
 */
export function Meter({ label, value, detail, tone = "rule", invert = false }) {
  if (value == null) {
    return (
      <div className="meter">
        <div className="meter-head">
          <span>{label}</span>
          <span className="meter-empty">no data yet</span>
        </div>
        <div className="track" />
        {detail && <div className="meter-detail">{detail}</div>}
      </div>
    );
  }
  const percent = Math.round(value * 100);
  // For a metric where low is good (an override rate), colour by band rather than by
  // magnitude, so a small bar does not read as a small problem.
  const band = invert
    ? percent >= 25 ? "warn" : percent >= 10 ? "caution" : "ok"
    : tone;
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <strong>{percent}%</strong>
      </div>
      <div className="track" title={`${label}: ${percent}%`}>
        <div className={`fill ${band}`} style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
      {detail && <div className="meter-detail">{detail}</div>}
    </div>
  );
}

/** Magnitude by category. Every bar is directly labelled, so there is no axis to read. */
export function BarChart({ data, max, color, sort = false }) {
  let entries = Object.entries(data || {});
  if (!entries.length) return <p className="empty">Nothing to show.</p>;
  if (sort) entries = entries.sort((a, b) => b[1] - a[1]);
  const ceiling = max || Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div>
      {entries.map(([label, value]) => (
        <div className="hbar" key={label} title={`${label}: ${value.toLocaleString()}`}>
          <span>{label}</span>
          <div className="track">
            <div
              className="fill"
              style={{ width: `${(value / ceiling) * 100}%`, background: color }}
            />
          </div>
          <span className="count">{value.toLocaleString()}</span>
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


/**
 * One thing worth acting on.
 *
 * Status carries a glyph and a word as well as a colour, because colour alone fails for
 * CVD readers, in print, and under forced-colors mode.
 */
export function Attention({ items }) {
  if (!items?.length) {
    return (
      <div className="attention ok">
        <span className="glyph">OK</span>
        <div>
          <strong>Nothing needs attention</strong>
          <div className="sub" style={{ margin: 0 }}>
            Every change is decided and the corpus verified clean.
          </div>
        </div>
      </div>
    );
  }
  const glyph = { error: "!!", warn: "!", info: "i" };
  const word = { error: "Action required", warn: "Attention", info: "For information" };
  return (
    <div>
      {items.map((item, index) => (
        <div className={`attention ${item.severity}`} key={index}>
          <span className="glyph" aria-label={word[item.severity]}>
            {glyph[item.severity] || "i"}
          </span>
          <div>
            <strong>{item.title}</strong>
            <div className="sub" style={{ margin: 0 }}>{item.detail}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
