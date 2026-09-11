import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import { NAV } from "./nav";
import DocumentsPage from "./DocumentsPage";
import HelpPage from "./HelpPage";
import MetricsPage from "./MetricsPage";
import ReviewPage from "./ReviewPage";
import RulebookPage from "./RulebookPage";
import RunPage from "./RunPage";
import SourcesPage from "./SourcesPage";
import TeamsPage from "./TeamsPage";
import VerifyPage from "./VerifyPage";
import WorkflowPage from "./WorkflowPage";

export default function App() {
  const [run, setRun] = useState(null);
  const [health, setHealth] = useState(null);
  const [counts, setCounts] = useState({ pending: 0, candidates: 0 });

  // Seed with the last run so no screen is empty on load.
  const reload = useCallback(async () => {
    let latest = null;
    try {
      latest = await api.latestRun();
      setRun(latest);
    } catch {
      setRun(null); // no runs yet is a normal state, not an error
    }
    // Candidate rules are counted separately: they are waiting on a different person
    // doing a different job, and folding them into one number would hide both.
    let candidates = 0;
    try {
      candidates = (await api.candidates({ status: "proposed" })).total;
    } catch {
      candidates = 0;
    }
    setCounts({ pending: latest?.changes?.pending ?? 0, candidates });
  }, []);

  useEffect(() => {
    reload();
    api.health().then(setHealth).catch(() => setHealth(null));
  }, [reload]);

  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">
          TermGuard
          <small>
            {health ? `${health.storage} storage · ${health.database}` : "connecting…"}
          </small>
        </div>

        <div className="nav">
          {NAV.map((group, index) => (
            <div className="nav-group" key={group.title || `top-${index}`}>
              {group.title && (
                <div className="nav-title" title={group.hint}>{group.title}</div>
              )}
              {group.items.map((item) => (
                <NavLink to={item.to} key={item.to}>
                  <span>{item.label}</span>
                  {counts[item.badge] > 0 && (
                    <span className="nav-badge">{counts[item.badge]}</span>
                  )}
                </NavLink>
              ))}
            </div>
          ))}
        </div>
      </nav>

      <main className="main">
        <div className="topbar">
          {run && (
            <span className="topbar-run">
              Run {run.run_id} · {run.changes?.pending ?? 0} pending
            </span>
          )}
          <NavLink to="/help" className="help-link" title="How to use TermGuard">
            <span aria-hidden="true">?</span> Help
          </NavLink>
        </div>
        <Routes>
          <Route path="/" element={<Navigate to="/metrics" replace />} />
          <Route path="/metrics" element={<MetricsPage />} />
          <Route path="/rulebook" element={<RulebookPage />} />
          <Route path="/sources" element={<SourcesPage />} />
          <Route path="/run" element={<RunPage run={run} reload={reload} />} />
          <Route path="/review" element={<ReviewPage run={run} reload={reload} />} />
          <Route path="/verify" element={<VerifyPage run={run} />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/help" element={<HelpPage />} />
          <Route path="/teams" element={<TeamsPage run={run} reload={reload} />} />
          <Route path="/workflow" element={<WorkflowPage run={run} reload={reload} />} />
        </Routes>
      </main>
    </div>
  );
}
