import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { Badge, Empty, ErrorBanner } from "./components";

/**
 * Teams, membership and cover.
 *
 * Teams are derived from the rulebook rather than maintained beside it: every rule
 * already names an owner, so the owner strings *are* the team list. Showing which rules a
 * team owns on the same row is the point — it is what makes routing legible instead of
 * magic.
 */
export default function TeamsPage({ run, reload }) {
  const [teams, setTeams] = useState([]);
  const [orgs, setOrgs] = useState([]);
  const [participants, setParticipants] = useState([]);
  const [routed, setRouted] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState({});
  const [cover, setCover] = useState({ who: "", to: "", reason: "" });

  const load = useCallback(async () => {
    try {
      const [t, o, p] = await Promise.all([api.teams(), api.orgs(), api.participants()]);
      setTeams(t);
      setOrgs(o);
      const described = await Promise.all(
        p.map((row) => api.describeParticipant(row.name).catch(() => row))
      );
      setParticipants(described);
      setError(null);
    } catch (caught) {
      setError(caught);
    }
  }, []);

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

  const humans = participants.filter((p) => p.kind === "human");

  return (
    <>
      <h1>Teams</h1>
      <p className="sub">
        {orgs.map((o) => o.name).join(", ") || "No organization yet"} ·{" "}
        {teams.length} team{teams.length === 1 ? "" : "s"} · {participants.length} people
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {teams.length === 0 ? (
        <div className="card">
          <p className="sub">
            No teams yet. They are derived from the rulebook — every rule names an owner,
            and those owner strings become the teams. Nothing to maintain twice.
          </p>
          <button className="primary" disabled={busy} onClick={() => act(api.seedTeams)}>
            Create teams from the rulebook
          </button>
        </div>
      ) : (
        <>
          <div className="card" style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <button disabled={busy} onClick={() => act(api.seedTeams)}>
              Sync teams with the rulebook
            </button>
            {run && (
              <button
                className="primary"
                disabled={busy}
                onClick={() => act(async () => setRouted(await api.route(run.run_id)))}
              >
                Route this run by rule owner
              </button>
            )}
            {routed && (
              <span className="sub" style={{ margin: 0 }}>
                routed {routed.total}
                {routed.unroutable > 0 && (
                  <strong> · {routed.unroutable} unroutable</strong>
                )}
              </span>
            )}
          </div>

          {routed && Object.keys(routed.owners_without_a_team || {}).length > 0 && (
            <div className="attention warn">
              <span className="glyph">!</span>
              <div>
                <strong>Some rule owners have no team</strong>
                <div className="sub" style={{ margin: 0 }}>
                  {Object.entries(routed.owners_without_a_team)
                    .map(([owner, rules]) => `${owner} (${rules.join(", ")})`)
                    .join("; ")}{" "}
                  — that work would have nowhere to go.
                </div>
              </div>
            </div>
          )}

          {teams.map((team) => (
            <div className="card" key={team.team_id}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <div>
                  <strong>{team.name}</strong>{" "}
                  <code className="sub">{team.slug}</code>
                </div>
                <div className="sub">
                  owns {team.owns_rules.join(", ") || "no rules"}
                </div>
              </div>

              <table style={{ marginTop: 10 }}>
                <tbody>
                  {team.members.length === 0 ? (
                    <tr>
                      <td className="sub">
                        Nobody on this team — work routed here would sit in an unwatched pool.
                      </td>
                    </tr>
                  ) : (
                    team.members.map((member) => (
                      <tr key={member.name}>
                        <td style={{ width: 230 }}>{member.name}</td>
                        <td style={{ width: 90 }}>
                          {member.lead ? <Badge kind="rule">lead</Badge> : <span className="sub">member</span>}
                        </td>
                        <td>
                          <span className={`badge ${member.kind === "agent" ? "ai" : "plain"}`}>
                            {member.kind}
                          </span>
                        </td>
                        <td style={{ textAlign: "right" }}>
                          <button
                            disabled={busy}
                            onClick={() => act(() => api.removeMember(team.slug, member.name))}
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>

              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                <select
                  value={adding[team.slug]?.name || ""}
                  onChange={(e) =>
                    setAdding({ ...adding, [team.slug]: { ...adding[team.slug], name: e.target.value } })
                  }
                  style={{ maxWidth: 240 }}
                >
                  <option value="">Add a participant…</option>
                  {participants
                    .filter((p) => !team.members.some((m) => m.name === p.name))
                    .map((p) => (
                      <option key={p.name} value={p.name}>{p.name}</option>
                    ))}
                </select>
                <select
                  value={adding[team.slug]?.role || "member"}
                  onChange={(e) =>
                    setAdding({ ...adding, [team.slug]: { ...adding[team.slug], role: e.target.value } })
                  }
                >
                  <option value="member">member</option>
                  <option value="lead">lead</option>
                </select>
                <button
                  disabled={busy || !adding[team.slug]?.name}
                  onClick={() =>
                    act(async () => {
                      await api.addMember(team.slug, {
                        participant: adding[team.slug].name,
                        role: adding[team.slug].role || "member",
                      });
                      setAdding({ ...adding, [team.slug]: {} });
                    })
                  }
                >
                  Add
                </button>
              </div>
            </div>
          ))}

          <h2>Cover</h2>
          <div className="card">
            <p className="sub">
              Cover does not move anyone's assignments. It changes who the effective owner
              is while it is in force, and lapses on its own — reassigning a hundred
              changes because somebody took a week off, then reassigning them back, is how
              work gets lost.
            </p>
            <table>
              <thead>
                <tr><th>Person</th><th>Teams</th><th>Status</th><th></th></tr>
              </thead>
              <tbody>
                {humans.map((person) => (
                  <tr key={person.name}>
                    <td>{person.name}</td>
                    <td className="sub">
                      {(person.teams || []).map((t) => t.slug).join(", ") || "—"}
                    </td>
                    <td>
                      {person.away ? (
                        <Badge kind="warn">away · covered by {person.covered_by}</Badge>
                      ) : (person.covering_for || []).length ? (
                        <Badge kind="rule">covering {person.covering_for.join(", ")}</Badge>
                      ) : (
                        <span className="sub">available</span>
                      )}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {person.away && (
                        <button
                          disabled={busy}
                          onClick={() => act(() => api.endDelegation(person.name))}
                        >
                          End cover
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "center" }}>
              <select value={cover.who} onChange={(e) => setCover({ ...cover, who: e.target.value })}>
                <option value="">Who is away…</option>
                {humans.filter((p) => !p.away).map((p) => (
                  <option key={p.name} value={p.name}>{p.name}</option>
                ))}
              </select>
              <span className="sub">covered by</span>
              <select value={cover.to} onChange={(e) => setCover({ ...cover, to: e.target.value })}>
                <option value="">…</option>
                {humans.filter((p) => p.name !== cover.who).map((p) => (
                  <option key={p.name} value={p.name}>{p.name}</option>
                ))}
              </select>
              <input
                type="text" placeholder="reason" value={cover.reason}
                onChange={(e) => setCover({ ...cover, reason: e.target.value })}
              />
              <button
                disabled={busy || !cover.who || !cover.to}
                onClick={() =>
                  act(async () => {
                    await api.delegate(cover.who, { to: cover.to, reason: cover.reason || null });
                    setCover({ who: "", to: "", reason: "" });
                  })
                }
              >
                Set cover
              </button>
            </div>
          </div>
        </>
      )}
    </>
  );
}
