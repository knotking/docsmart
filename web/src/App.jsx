import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import DocumentsPage from "./DocumentsPage";
import MetricsPage from "./MetricsPage";
import ReviewPage from "./ReviewPage";
import RulebookPage from "./RulebookPage";
import RunPage from "./RunPage";
import VerifyPage from "./VerifyPage";
import WorkflowPage from "./WorkflowPage";

export default function App() {
  const [run, setRun] = useState(null);
  const [health, setHealth] = useState(null);

  // Seed with the last run so no screen is empty on load.
  const reload = useCallback(async () => {
    try {
      setRun(await api.latestRun());
    } catch {
      setRun(null); // no runs yet is a normal state, not an error
    }
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
          <NavLink to="/metrics">Metrics</NavLink>
          <NavLink to="/rulebook">Rulebook</NavLink>
          <NavLink to="/run">Run</NavLink>
          <NavLink to="/review">Review</NavLink>
          <NavLink to="/verify">Verify</NavLink>
          <NavLink to="/documents">Documents</NavLink>
          <NavLink to="/workflow">Workflow</NavLink>
        </div>
        {run && (
          <div className="sub" style={{ padding: "18px 20px 0", fontSize: 11 }}>
            Run {run.run_id}
            <br />
            {run.changes?.pending ?? 0} pending decisions
          </div>
        )}
      </nav>

      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/metrics" replace />} />
          <Route path="/metrics" element={<MetricsPage />} />
          <Route path="/rulebook" element={<RulebookPage />} />
          <Route path="/run" element={<RunPage run={run} reload={reload} />} />
          <Route path="/review" element={<ReviewPage run={run} reload={reload} />} />
          <Route path="/verify" element={<VerifyPage run={run} />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/workflow" element={<WorkflowPage run={run} reload={reload} />} />
        </Routes>
      </main>
    </div>
  );
}
