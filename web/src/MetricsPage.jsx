import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { Attention, BarChart, Empty, ErrorBanner, Meter, SplitBar } from "./components";

/**
 * The landing screen: where the corpus stands, and whether the machine is earning trust.
 *
 * Ordered by what someone opening the tool actually needs — what needs doing, then the
 * posture, then the two questions a regulated audience asks about automation: how much
 * of this did a machine decide, and how often were reviewers forced to overrule it.
 */
export default function MetricsPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setData(await api.metrics());
      setError(null);
    } catch (caught) {
      setError(caught);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) return <Empty>Loading metrics…</Empty>;
  if (!data) {
    return (
      <>
        <h1>Metrics</h1>
        <ErrorBanner error={error} onDismiss={() => setError(null)} />
        <Empty>No data yet. Start a run first.</Empty>
      </>
    );
  }

  const { posture, ai_trust: trust, throughput, rule_health: rules, attention } = data;
  const stages = posture.documents_by_stage || {};

  return (
    <>
      <h1>Metrics</h1>
      <p className="sub">
        Run {data.run_id ?? "—"} · generated {new Date(data.generated_at).toLocaleString()}{" "}
        · <a href="#" onClick={(e) => { e.preventDefault(); load(); }}>refresh</a>
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <h2>Needs attention</h2>
      <Attention items={attention} />

      <h2>Corpus posture</h2>
      <div className="kpi">
        <div className="card">
          <div className="stat-label">Documents</div>
          <div className="hero">{posture.documents}</div>
          <div className="sub" style={{ margin: 0 }}>
            {posture.versions} versions · {(posture.bytes_stored / 1_048_576).toFixed(1)} MB
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Verified clean</div>
          <div className="hero">
            {posture.verified_share == null ? "—" : Math.round(posture.verified_share * 100)}
            <span className="hero-unit">%</span>
          </div>
          <div className="sub" style={{ margin: 0 }}>
            {stages.verified || 0} of {posture.documents} documents
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Pending decisions</div>
          <div className="hero">{posture.changes.pending}</div>
          <div className="sub" style={{ margin: 0 }}>
            of {posture.changes.total} changes
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Runs signed off</div>
          <div className="hero">{posture.signed_off_runs}</div>
          <div className="sub" style={{ margin: 0 }}>
            {posture.runs.total} run{posture.runs.total === 1 ? "" : "s"} total
          </div>
        </div>
      </div>

      <div className="card">
        <h3>Documents by stage</h3>
        <BarChart
          data={Object.fromEntries(Object.entries(stages).filter(([, v]) => v > 0))}
          color="var(--rule)"
        />
      </div>

      <h2>How much a machine decided</h2>
      <div className="card">
        <SplitBar
          title="Who recorded each decision"
          left={throughput.human_decisions}
          right={throughput.agent_decisions}
          leftLabel="Human reviewer"
          rightLabel="Agent (under policy)"
        />
        <div className="row" style={{ marginTop: 4 }}>
          <Meter
            label="Automation rate"
            value={throughput.automation_rate}
            tone="ai"
            detail="Share of decisions an agent recorded under a policy clause. Everything else reached a person."
          />
          <Meter
            label="Deterministic share of changes"
            value={throughput.deterministic_share}
            tone="rule"
            detail="Changes written by the rule engine rather than the model."
          />
        </div>
        {Object.keys(trust.agent_decided.by_clause).length > 0 && (
          <>
            <h3 style={{ marginTop: 18 }}>Agent decisions by policy clause</h3>
            <BarChart data={trust.agent_decided.by_clause} color="var(--ai)" sort />
            <p className="sub" style={{ marginTop: 8, marginBottom: 0 }}>
              Every agent decision names the clause that authorized it.
              {trust.agent_decided.unattributed > 0 ? (
                <strong> {trust.agent_decided.unattributed} have none — investigate.</strong>
              ) : (
                " None are unattributed."
              )}
            </p>
          </>
        )}
      </div>

      <h2>Trust in the model's proposals</h2>
      <div className="row">
        <div className="card">
          <Meter
            label="Reviewers upheld the model"
            value={trust.acceptance_rate}
            tone="ok"
            detail={`${trust.judged_by_humans} of ${trust.ai_proposals} AI proposals have a human verdict.`}
          />
          <Meter
            label="Reviewers overruled the model"
            value={trust.override_rate}
            invert
            detail="Rejected or reworded. A rising override rate means a rule's context note needs work."
          />
          <dl className="kv" style={{ marginTop: 14 }}>
            <dt>Accepted</dt><dd>{trust.accepted}</dd>
            <dt>Reworded</dt><dd>{trust.edited}</dd>
            <dt>Rejected</dt><dd>{trust.rejected}</dd>
            <dt>Model said keep</dt><dd>{trust.model_said_keep}</dd>
            <dt>Refused by validation</dt>
            <dd>
              {trust.escalated_by_validation}
              <span className="sub"> — containment fired, sent to a human</span>
            </dd>
          </dl>
        </div>
        <div className="card">
          <h3>Who did the work</h3>
          <table>
            <thead>
              <tr>
                <th>Participant</th>
                <th>Kind</th>
                <th className="num">Decisions</th>
                <th className="num">Accepted</th>
                <th className="num">Overruled</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(throughput.by_participant).map(([name, entry]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td>
                    <span className={`badge ${entry.kind === "agent" ? "ai" : "plain"}`}>
                      {entry.kind}
                    </span>
                  </td>
                  <td className="num">{entry.decisions}</td>
                  <td className="num">{entry.accepted}</td>
                  <td className="num">{entry.rejected + entry.edited}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="sub" style={{ marginTop: 10, marginBottom: 0 }}>
            {throughput.decisions_per_hour_estimate
              ? `Observed human pace: ${throughput.decisions_per_hour_estimate} decisions/hour.`
              : "Not enough elapsed time to measure a review pace — a rate extrapolated from a batch script would be fiction."}
          </p>
        </div>
      </div>

      <h2>Rule health</h2>
      <div className="card">
        <p className="sub">
          Sorted by how often reviewers overturned the rule. A high override rate is a
          defect in the rulebook — usually an approved term that is wrong in a context the
          rule does not describe — not a problem with the reviewers.
        </p>
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th className="num">Hits</th>
              <th className="num">Outside body</th>
              <th className="num">Needs judgment</th>
              <th className="num">Judged</th>
              <th className="num">Overturned</th>
              <th style={{ width: 140 }}>Override rate</th>
            </tr>
          </thead>
          <tbody>
            {rules.map((rule) => (
              <tr key={rule.rule_id}>
                <td><code>{rule.rule_id}</code></td>
                <td className="num">{rule.hits}</td>
                <td className="num">{rule.outside_body || "—"}</td>
                <td className="num">{rule.needs_judgment || "—"}</td>
                <td className="num">{rule.judged}</td>
                <td className="num">{rule.rejected + rule.edited}</td>
                <td>
                  {rule.override_rate == null ? (
                    <span className="sub">no verdicts</span>
                  ) : (
                    <div className="track" style={{ background: "var(--bg)", border: "1px solid var(--line)", borderRadius: 3, height: 8, overflow: "hidden" }}>
                      <div
                        style={{
                          width: `${Math.max(2, rule.override_rate * 100)}%`,
                          height: "100%",
                          background:
                            rule.override_rate >= 0.25 ? "var(--warn)"
                            : rule.override_rate >= 0.1 ? "#9a7b1f" : "var(--ok)",
                        }}
                        title={`${rule.rule_id}: ${Math.round(rule.override_rate * 100)}% overturned`}
                      />
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
