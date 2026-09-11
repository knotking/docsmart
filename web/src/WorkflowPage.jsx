import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { Badge, Empty, ErrorBanner } from "./components";

/**
 * Who is working this run, what the agent is permitted to do, and who may sign it off.
 *
 * The sign-off panel deliberately shows *why* someone is ineligible rather than just
 * hiding the button. "Dana can approve, Alice cannot because she reviewed 95 changes in
 * this run" is the control working, and a reviewer who cannot see the reason assumes the
 * tool is broken.
 */
export default function WorkflowPage({ run, reload }) {
  const [participants, setParticipants] = useState([]);
  const [policy, setPolicy] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState("human");
  const [newRole, setNewRole] = useState("reviewer");
  const [approver, setApprover] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      const [people, pol] = await Promise.all([api.participants(), api.policy()]);
      setParticipants(people);
      setPolicy(pol);
      if (run) {
        const ready = await api.signoffReadiness(run.run_id);
        setReadiness(ready);
        if (ready.eligible_approvers.length && !approver) {
          setApprover(ready.eligible_approvers[0]);
        }
      }
    } catch (caught) {
      setError(caught);
    }
    // `approver` intentionally excluded: refreshing must not fight the user's selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
      reload?.();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };

  const reviewers = participants.filter(
    (p) => p.kind === "human" && p.roles.includes("reviewer")
  );

  return (
    <>
      <h1>Workflow</h1>
      <p className="sub">
        Participants, what an agent is permitted to decide on its own, and who may sign
        this run off.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <h2>Participants</h2>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Name</th><th>Kind</th><th>Roles</th>
              <th className="num">Decisions</th><th>Model / policy</th>
            </tr>
          </thead>
          <tbody>
            {participants.length === 0 ? (
              <tr><td colSpan={5} className="sub">No participants registered yet.</td></tr>
            ) : participants.map((p) => (
              <tr key={p.participant_id}>
                <td>{p.name}</td>
                <td>
                  <Badge kind={p.kind === "agent" ? "ai" : "plain"}>{p.kind}</Badge>
                </td>
                <td>
                  {p.roles.map((role) => (
                    <span key={role} className="badge plain" style={{ marginRight: 4 }}>
                      {role}
                    </span>
                  ))}
                </td>
                <td className="num">{p.decisions}</td>
                <td className="mono sub">
                  {p.model || "—"}{p.policy_hash ? ` · ${p.policy_hash}` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div style={{ display: "flex", gap: 8, marginTop: 14, alignItems: "center" }}>
          <input
            type="text" placeholder="name@example.com" value={newName}
            onChange={(e) => setNewName(e.target.value)} style={{ maxWidth: 240 }}
          />
          <select value={newKind} onChange={(e) => setNewKind(e.target.value)}>
            <option value="human">human</option>
            <option value="agent">agent</option>
          </select>
          <select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
            <option value="reviewer">reviewer</option>
            <option value="approver">approver</option>
            <option value="observer">observer</option>
          </select>
          <button
            disabled={busy || !newName.trim()}
            onClick={() => act(async () => {
              await api.createParticipant({ name: newName.trim(), kind: newKind, roles: [newRole] });
              setNewName("");
            })}
          >
            Add
          </button>
        </div>
      </div>

      <h2>Agent authority</h2>
      <div className="card">
        {!policy ? <Empty>Loading policy…</Empty> : (
          <>
            <p className="sub">
              Policy <code>{policy.hash}</code> · approved by {policy.approved_by || "—"} ·{" "}
              {policy.clauses.length} clause{policy.clauses.length === 1 ? "" : "s"}.
              Anything no clause covers goes to a human; that default is not configurable.
            </p>
            <table>
              <thead>
                <tr>
                  <th>Clause</th><th>Rules</th><th>Scope</th>
                  <th>Max risk</th><th>Confirm</th><th>Why</th>
                </tr>
              </thead>
              <tbody>
                {policy.clauses.map((clause) => (
                  <tr key={clause.id}>
                    <td><code>{clause.id}</code></td>
                    <td className="mono">{clause.rules.join(", ") || "any"}</td>
                    <td className="sub">
                      {clause.classifications.join("/")} · {clause.mechanisms.join("/")}
                      {clause.parts.length ? ` · ${clause.parts.join("/")}` : ""}
                    </td>
                    <td><Badge kind={clause.max_risk === "low" ? "ok" : "warn"}>{clause.max_risk}</Badge></td>
                    <td>{clause.requires_human_confirm ? <Badge kind="warn">human</Badge> : "—"}</td>
                    <td className="sub" style={{ maxWidth: 340 }}>{clause.rationale}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {run && (
              <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                <button
                  disabled={busy}
                  onClick={() => act(() => api.agentDispose(run.run_id, { dry_run: true }))}
                >
                  Preview what the agent could decide
                </button>
                <button
                  disabled={busy}
                  onClick={() => act(() => api.agentDispose(run.run_id, {}))}
                >
                  Let the agent decide those
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {run && (
        <>
          <h2>Assignment</h2>
          <div className="card">
            <p className="sub">
              Spread the undecided changes across reviewers. Keeping a whole document with
              one reviewer beats even load — someone who has read the document makes
              faster, better calls on the rest of it.
            </p>
            <div style={{ display: "flex", gap: 8 }}>
              {["by_file", "by_rule", "round_robin"].map((strategy) => (
                <button
                  key={strategy}
                  disabled={busy || reviewers.length === 0}
                  onClick={() => act(() => api.assign(run.run_id, {
                    participants: reviewers.map((p) => p.name), strategy,
                  }))}
                >
                  Assign {strategy.replace("_", " ")}
                </button>
              ))}
            </div>
            {reviewers.length === 0 && (
              <p className="sub" style={{ marginTop: 10, marginBottom: 0 }}>
                Add at least one human reviewer first.
              </p>
            )}
          </div>

          <h2>Sign-off</h2>
          <div className="card">
            {!readiness ? <Empty>Loading…</Empty> : (
              <>
                <div style={{ marginBottom: 12 }}>
                  {readiness.ready
                    ? <Badge kind="ok">Ready for sign-off</Badge>
                    : <Badge kind="warn">Not ready</Badge>}
                </div>
                {readiness.blockers.length > 0 && (
                  <ul className="sub" style={{ marginTop: 0 }}>
                    {readiness.blockers.map((b, i) => <li key={i}>{b}</li>)}
                  </ul>
                )}

                {readiness.unconfirmed_agent_decisions > 0 && (
                  <div style={{ marginBottom: 12 }}>
                    <button
                      disabled={busy || reviewers.length === 0}
                      onClick={() => act(() => api.confirmAgentBatch(run.run_id, {
                        participant: reviewers[0]?.name,
                      }))}
                    >
                      Confirm {readiness.unconfirmed_agent_decisions} agent decision(s)
                    </button>
                  </div>
                )}

                <h3>Who may sign off</h3>
                {readiness.eligible_approvers.length === 0 ? (
                  <p className="sub">
                    Nobody is eligible. An approver must hold the approver role, must not
                    have decided any change in this run, and may not be an agent.
                  </p>
                ) : (
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <select value={approver} onChange={(e) => setApprover(e.target.value)}
                            style={{ maxWidth: 240 }}>
                      {readiness.eligible_approvers.map((name) => (
                        <option key={name} value={name}>{name}</option>
                      ))}
                    </select>
                    <input
                      type="text" placeholder="note (optional)" value={note}
                      onChange={(e) => setNote(e.target.value)}
                    />
                    <button
                      className="primary" disabled={busy || !readiness.ready || !approver}
                      onClick={() => act(async () => {
                        await api.signOff(run.run_id, {
                          participant: approver, decision: "approved", note: note || null,
                        });
                        setNote("");
                      })}
                    >
                      Approve run
                    </button>
                  </div>
                )}

                <h3 style={{ marginTop: 18 }}>Ineligible, and why</h3>
                <table>
                  <tbody>
                    {participants
                      .filter((p) => !readiness.eligible_approvers.includes(p.name))
                      .map((p) => (
                        <tr key={p.participant_id}>
                          <td style={{ width: 220 }}>{p.name}</td>
                          <td className="sub">
                            {p.kind === "agent"
                              ? "an agent cannot sign off — sign-off is a human accountability act"
                              : !p.roles.includes("approver")
                              ? "does not hold the approver role"
                              : `recorded ${p.decisions} decision(s) in this run (maker-checker)`}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>

                {readiness.signed.length > 0 && (
                  <>
                    <h3 style={{ marginTop: 18 }}>Signed</h3>
                    <table>
                      <thead>
                        <tr><th>Participant</th><th>Decision</th><th>When</th><th>Hashes</th></tr>
                      </thead>
                      <tbody>
                        {readiness.signed.map((s, i) => (
                          <tr key={i}>
                            <td>{s.participant}</td>
                            <td>
                              <Badge kind={s.decision === "approved" ? "ok" : "warn"}>
                                {s.decision}
                              </Badge>
                            </td>
                            <td className="sub">{new Date(s.signed_at).toLocaleString()}</td>
                            <td className="mono sub">
                              rulebook {s.rulebook_hash}<br />policy {s.policy_hash || "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </>
                )}
              </>
            )}
          </div>
        </>
      )}
    </>
  );
}
