const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8010';

async function fetchAPI(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `API error ${res.status}`);
  }
  return res.json();
}

function authHeader(token) {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export const api = {
  // System
  getStatus: () => fetchAPI('/api/v1/status'),
  getSystemMode: () => fetchAPI('/api/v1/system/mode'),
  setSystemMode: (mode, reason) => fetchAPI('/api/v1/system/mode', {
    method: 'POST', body: JSON.stringify({ mode, reason })
  }),

  // Config
  getConfig: () => fetchAPI('/api/v1/config'),
  saveConfig: (data) => fetchAPI('/api/v1/config', { method: 'POST', body: JSON.stringify(data) }),

  // Incidents
  getIncidents: (status) => fetchAPI(`/api/v1/incidents${status ? `?status=${status}` : ''}`),
  getIncidentContext: (id) => fetchAPI(`/api/v1/incidents/${id}/context`),
  approveEscalation: (id) => fetchAPI(`/api/v1/escalations/${id}/approve`, { method: 'POST' }),
  rollbackIncident: (id) => fetchAPI(`/api/v1/incidents/${id}/rollback`, { method: 'POST' }),

  // Stage 2: AI patch proposals (proposal only — there is deliberately no apply/deploy call).
  // Requests never carry a confidence or route; the backend computes both from the stored report.
  getPatchProposals: (id) => fetchAPI(`/api/v1/incidents/${id}/patch-proposals`),
  startPatchProposal: (id, retry = false) => fetchAPI(`/api/v1/incidents/${id}/patch-proposals`, {
    method: 'POST', body: JSON.stringify({ retry })
  }),
  approvePatchGeneration: (id, reason) => fetchAPI(`/api/v1/incidents/${id}/patch-proposals/approve`, {
    method: 'POST', body: JSON.stringify({ reason: reason || null })
  }),
  rejectPatchGeneration: (id, reason) => fetchAPI(`/api/v1/incidents/${id}/patch-proposals/reject`, {
    method: 'POST', body: JSON.stringify({ reason: reason || null })
  }),

  // Stages 3-4: human approval of a validated proposal -> GitHub PR -> controlled deployment ->
  // recovery verification. The browser sends only the proposal id it reviewed (and a reason);
  // the server re-checks everything. `token` is the optional approver token (APPROVER_TOKENS).
  getRemediation: (id) => fetchAPI(`/api/v1/incidents/${id}/remediation`),
  approveRemediation: (id, proposalId, reason, token) => fetchAPI(`/api/v1/incidents/${id}/remediation/approve`, {
    method: 'POST', headers: authHeader(token), body: JSON.stringify({ proposal_id: proposalId, reason: reason || null })
  }),
  rejectRemediation: (id, proposalId, reason, token) => fetchAPI(`/api/v1/incidents/${id}/remediation/reject`, {
    method: 'POST', headers: authHeader(token), body: JSON.stringify({ proposal_id: proposalId, reason })
  }),
  advanceRemediation: (id, token) => fetchAPI(`/api/v1/incidents/${id}/remediation/advance`, {
    method: 'POST', headers: authHeader(token), body: '{}'
  }),
  closeIncident: (id, reason, token) => fetchAPI(`/api/v1/incidents/${id}/close`, {
    method: 'POST', headers: authHeader(token), body: JSON.stringify({ reason })
  }),

  // Metrics
  getMetrics: () => fetchAPI('/api/v1/metrics'),

  // Chaos
  injectChaos: (data) => fetchAPI('/api/v1/chaos/inject', { method: 'POST', body: JSON.stringify(data) }),

  // Prometheus
  checkPrometheusHealth: () => fetchAPI('/api/v1/prometheus/health'),
  getTargetHealth: () => fetchAPI('/api/v1/target/health'),

  // Service Account
  getServiceAccount: () => fetchAPI('/api/v1/service-account'),
  saveServiceAccount: (data) => fetchAPI('/api/v1/service-account', { method: 'POST', body: JSON.stringify(data) }),
  deleteServiceAccount: () => fetchAPI('/api/v1/service-account', { method: 'DELETE' }),

  // WebSocket
  listenEvents: (onEvent) => {
    const wsUrl = API_BASE.replace(/^http/, 'ws') + '/api/v1/events/ws';
    const ws = new WebSocket(wsUrl);
    ws.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        onEvent(event);
      } catch (err) {
        console.error('Failed to parse WS event:', err);
      }
    };
    // A WS failure is non-fatal (pages also poll), so warn instead of
    // console.error, which Next.js dev surfaces as an error overlay.
    let closedByUs = false;
    ws.onerror = () => {
      if (!closedByUs) console.warn(`WS unavailable at ${wsUrl}; relying on polling.`);
    };
    return () => {
      closedByUs = true;
      // Closing while still CONNECTING (e.g. StrictMode dev remount) fires an
      // error event; wait for open and close then instead.
      if (ws.readyState === WebSocket.CONNECTING) ws.onopen = () => ws.close();
      else ws.close();
    };
  },
};
