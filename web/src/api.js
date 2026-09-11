// Thin wrapper over the TermGuard API. Every number the dashboard shows comes from
// here; nothing is computed client-side that the API does not already report.

const BASE = "/api";

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* the body was not JSON; the status line is the best we have */
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

export const api = {
  health: () => request("/health"),

  latestRun: () => request("/runs/latest"),
  getRun: (id) => request(`/runs/${id}`),
  listRuns: () => request("/runs"),
  startRun: (body = {}) =>
    request("/runs", { method: "POST", body: JSON.stringify({ dry_run: true, actor: "dashboard", ...body }) }),

  hits: (runId, params = {}) =>
    request(`/runs/${runId}/hits?${new URLSearchParams(clean(params))}`),

  queue: (runId, params = {}) =>
    request(`/runs/${runId}/queue?${new URLSearchParams(clean(params))}`),
  decide: (changeId, body) =>
    request(`/changes/${changeId}/decision`, { method: "POST", body: JSON.stringify(body) }),
  decisionHistory: (changeId) => request(`/changes/${changeId}/decisions`),

  verify: (runId) => request(`/runs/${runId}/verify`, { method: "POST" }),

  documents: () => request("/documents"),
  versions: (id) => request(`/documents/${id}/versions`),
  timeline: (id) => request(`/documents/${id}/timeline`),
  integrity: () => request("/integrity"),

  rulebook: () => request("/rulebook"),
  saveRulebook: (body) => request("/rulebook", { method: "PUT", body: JSON.stringify(body) }),

  // --- metrics -------------------------------------------------------------
  metrics: (runId) => request(`/metrics${runId ? `?run_id=${runId}` : ""}`),
  metricsRules: (runId) => request(`/metrics/rules${runId ? `?run_id=${runId}` : ""}`),

  // --- workflow ------------------------------------------------------------
  policy: () => request("/policy"),
  participants: () => request("/participants"),
  createParticipant: (body) =>
    request("/participants", { method: "POST", body: JSON.stringify(body) }),
  assign: (runId, body) =>
    request(`/runs/${runId}/assign`, { method: "POST", body: JSON.stringify(body) }),
  claim: (changeId, body) =>
    request(`/changes/${changeId}/claim`, { method: "POST", body: JSON.stringify(body) }),
  releaseClaim: (changeId, participant) =>
    request(`/changes/${changeId}/claim?participant=${encodeURIComponent(participant)}`,
            { method: "DELETE" }),
  agentDispose: (runId, body) =>
    request(`/runs/${runId}/agent-dispose`, { method: "POST", body: JSON.stringify(body) }),
  confirmAgentBatch: (runId, body) =>
    request(`/runs/${runId}/confirm-agent-batch`, { method: "POST", body: JSON.stringify(body) }),
  signoffReadiness: (runId) => request(`/runs/${runId}/signoff`),
  signOff: (runId, body) =>
    request(`/runs/${runId}/signoff`, { method: "POST", body: JSON.stringify(body) }),

  // Plain URLs, for links and downloads.
  urls: {
    redlined: (runId, name) => `${BASE}/runs/${runId}/files/${encodeURIComponent(name)}/redlined`,
    final: (runId, name) => `${BASE}/runs/${runId}/files/${encodeURIComponent(name)}/final`,
    version: (docId, versionNo) => `${BASE}/documents/${docId}/versions/${versionNo}/download`,
    auditCsv: (runId) => `${BASE}/runs/${runId}/audit.csv`,
    events: (token) => `${BASE}/runs/stream/${token}`,
  },
};

function clean(params) {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== "" && value != null)
  );
}
