// Pure view logic for the self-healing pipeline strip and the Stage 3-4 panel.
// A stage is shown as completed ONLY when the backend's stored data proves it:
// the incident's status timeline, how it was created (source) and the
// remediation run's stage. Nothing here is guessed from elapsed time.

export const STAGES = [
  { key: 'target', label: 'Target App', icon: '🎯', desc: 'The monitored workload. It exposes /metrics and /health; chaos faults are injected here.' },
  { key: 'prometheus', label: 'Prometheus', icon: '📈', desc: 'Scrapes the target every 15s and evaluates alert rules (memory, CPU, restarts, up == 0).' },
  { key: 'alertmanager', label: 'AlertManager', icon: '🔔', desc: 'Groups firing alerts and posts them as a webhook to the AutoFixOps API.' },
  { key: 'api', label: 'AutoFixOps API', icon: '⚡', desc: 'Ingests the alert, de-duplicates it and creates an incident (INGESTED).' },
  { key: 'context', label: 'Context', icon: '🧩', desc: 'Collects memory/CPU telemetry from Prometheus and the alert labels (CONTEXT_BUILT).' },
  { key: 'diagnosis', label: 'Diagnosis', icon: '🧠', desc: 'Rule engine + runbook RAG + local LLM report, then a sandbox-validated patch proposal (DIAGNOSED).' },
  { key: 'policy', label: 'Policy', icon: '🛡', desc: 'Safety gates and the human approval of the exact validated proposal (POLICY_APPROVED).' },
  { key: 'remediation', label: 'Remediation', icon: '🔧', desc: 'GitHub pull request, then a controlled kubectl patch of the one approved value (a limit or a feature flag).' },
  { key: 'verification', label: 'Verification', icon: '✅', desc: 'Deployment, pods, health URL, alert and headroom must stay healthy for the stabilization window.' },
];

const IDX = Object.fromEntries(STAGES.map((s, i) => [s.key, i]));

// Status -> index of the last stage that status proves finished.
const DONE = { INGESTED: IDX.api, CONTEXT_BUILT: IDX.context, DIAGNOSED: IDX.diagnosis, POLICY_APPROVED: IDX.policy,
  REMEDIATING: IDX.policy, PENDING_PR_MERGE: IDX.policy, VERIFIED: IDX.verification, RESOLVED: IDX.verification };

// Remediation run stage -> index of the last finished stage (refines REMEDIATING / PENDING_PR_MERGE).
const RUN_DONE = { APPROVED: IDX.policy, PR_OPEN: IDX.policy, READY_TO_DEPLOY: IDX.policy, DEPLOYING: IDX.policy,
  VERIFYING: IDX.remediation, RESOLVED: IDX.verification };

function reached(incident) {
  const seen = (incident?.status_timeline || []).map((e) => e.status);
  return new Set([...seen, incident?.status].filter(Boolean));
}

/** One of idle | completed | active | warning | failed | skipped per stage. */
export function pipelineStates(incident, run = null) {
  const states = STAGES.map(() => 'idle');
  if (!incident?.status) return states;
  const statuses = reached(incident);
  const chaos = incident.source === 'chaos_injection';

  // Ingestion: an incident exists, so the API stage is done. For an Alertmanager webhook that
  // also proves Prometheus fired the rule and Alertmanager delivered it; a chaos injection
  // creates the incident directly, so those two stages never ran.
  states[IDX.target] = 'completed';
  states[IDX.prometheus] = chaos ? 'skipped' : 'completed';
  states[IDX.alertmanager] = chaos ? 'skipped' : 'completed';

  let done = Math.max(...[...statuses].map((s) => DONE[s] ?? IDX.api));
  if (run && RUN_DONE[run.stage] != null) done = Math.max(done, RUN_DONE[run.stage]);
  for (let i = IDX.api; i <= done; i++) states[i] = 'completed';
  const next = done + 1;

  const status = incident.status;
  if (status === 'FAILED') {
    if (next < STAGES.length) states[next] = 'failed';
    else states[IDX.verification] = 'failed';
    return states;
  }
  if (status === 'ESCALATED') {
    if (run && run.final && ['FAILED', 'ROLLED_BACK', 'ROLLBACK_FAILED'].includes(run.stage)) {
      // An approved remediation failed: show where, and that a human now owns the incident.
      const deployed = Boolean(run.deployment?.patched);
      states[IDX.policy] = 'completed';
      states[IDX.remediation] = deployed ? 'completed' : 'failed';
      states[IDX.verification] = deployed ? 'failed' : 'idle';
      return states;
    }
    states[IDX.policy] = 'warning';
    return states;
  }
  if (status === 'RESOLVED' && !statuses.has('VERIFIED')) {
    // closed by a human without automated remediation
    states[IDX.policy] = 'warning';
    states[IDX.remediation] = 'skipped';
    states[IDX.verification] = 'skipped';
    return states;
  }
  if (next < STAGES.length) states[next] = 'active';
  return states;
}

// ─── Chaos page ───

export const CHAOS_FAULTS = [
  { id: 'memory_leak', name: 'Memory Leak', icon: '🧠', severity: 'HIGH',
    kubernetes: { desc: 'The response cache stops evicting: memory grows 40 MB/min until the pod is OOMKilled, again after every restart.',
      outcome: 'memory alerts; AutoFixOps proposes turning the cache off (RESPONSE_CACHE_ENABLED=false in deployment.yaml), never a larger limit' },
    local: { desc: 'Calls /leak on the local process once (+10 MB).', outcome: 'a ChaosInjection incident' } },
  { id: 'cpu_spike', name: 'CPU Spike', icon: '🔥', severity: 'MEDIUM',
    kubernetes: { desc: 'Background work needs 130% of the CPU limit: its backlog grows, readiness fails and the pod leaves service (Running, 0/1 Ready).',
      outcome: 'TargetAppDown alert; CPU limit increase (2x) after approval and merge' },
    local: { desc: 'Calls /cpu on the local process (a busy loop in one thread).', outcome: 'a ChaosInjection incident' } },
  { id: 'crash_loop', name: 'Crash Loop', icon: '💀', severity: 'HIGH',
    kubernetes: { desc: 'Start-up needs more memory than the limit: OOMKilled at every start, CrashLoopBackOff.',
      outcome: 'TargetAppDown alert; memory limit increase after approval and merge' },
    local: { desc: 'Calls /_chaos/crash on the local process (exits once).', outcome: 'a ChaosInjection incident' } },
];

/** "kubernetes" when the API reports an available, chaos-enabled Kubernetes target; else "local". */
export function chaosMode(k8s) {
  return k8s?.chaos_enabled === true && k8s?.evidence?.available === true ? 'kubernetes' : 'local';
}

// ─── Stage 3-4 panel helpers ───

export const RUN_STAGE_INFO = {
  APPROVED: { label: 'APPROVED — PREPARING PULL REQUEST', color: 'var(--info)' },
  PR_OPEN: { label: 'PULL REQUEST OPEN — WAITING FOR MERGE', color: 'var(--info)' },
  READY_TO_DEPLOY: { label: 'READY TO DEPLOY', color: 'var(--info)' },
  DEPLOYING: { label: 'DEPLOYING', color: 'var(--info)' },
  VERIFYING: { label: 'VERIFYING RECOVERY', color: 'var(--info)' },
  RESOLVED: { label: 'RESOLVED — RECOVERY VERIFIED', color: 'var(--success)' },
  REJECTED: { label: 'REJECTED BY HUMAN', color: 'var(--error)' },
  FAILED: { label: 'FAILED — NOT DEPLOYED', color: 'var(--error)' },
  ROLLED_BACK: { label: 'ROLLED BACK', color: 'var(--warning)' },
  ROLLBACK_FAILED: { label: 'ROLLBACK FAILED — MANUAL ACTION REQUIRED', color: 'var(--error)' },
};

const BLOCKED_TEXT = {
  GITHUB_NOT_CONFIGURED: 'GitHub is not configured. Set GITHUB_TOKEN and GITHUB_REPO (or a service account), or set REMEDIATION_REQUIRE_PR=false for a local-only demo, then retry.',
  DEPLOY_DISABLED: 'Automated deployment is off. Set DEPLOY_MODE=kubectl, DEPLOY_KUBE_CONTEXT, DEPLOY_ALLOWED_TARGETS and VERIFY_HEALTH_URL, restart the worker, then retry.',
  DEPLOY_CONFIG_INVALID: 'The deployment settings are incomplete or invalid.',
  VERIFY_CONFIG_INVALID: 'The verification settings are incomplete or invalid (VERIFY_HEALTH_URL is required).',
  TARGET_NOT_ALLOWLISTED: 'This Deployment is not in DEPLOY_ALLOWED_TARGETS.',
  SHADOW_MODE: 'Shadow mode never deploys. Set SHADOW_MODE=false and the system mode to ACTIVE to deploy.',
  SYSTEM_DISABLED: 'The kill switch is DISABLED.',
  TARGET_BUSY: 'Another remediation is deploying to the same target; this one will retry automatically.',
  CLUSTER_UNREACHABLE: 'kubectl could not reach the cluster (check DEPLOY_KUBE_CONTEXT and that Kubernetes is running).',
  BRANCH_CONFLICT: 'The remediation branch already exists with different content.',
  CI_PENDING: 'The PR is merged; deployment waits until GitHub Actions has passed on the PR commit.',
  DEPLOY_STATE_UNKNOWN: 'The cluster could not be read after a deploy step; the change is checked again before anything else happens.',
  PROPOSAL_CHANGED: 'The stored proposal no longer matches what was approved; nothing will be deployed.',
};

export function blockedText(code, detail) {
  if (!code) return '';
  return BLOCKED_TEXT[code] || detail || code.replace(/_/g, ' ').toLowerCase();
}

/** Ordered Stage 3-4 steps with a backend-confirmed state each. */
export function runSteps(run) {
  const steps = [
    { key: 'approval', label: 'Human approval' },
    { key: 'pr', label: 'GitHub pull request' },
    { key: 'merge', label: 'PR merged (exact patch)' },
    { key: 'deploy', label: 'Controlled deployment' },
    { key: 'verify', label: 'Recovery verification' },
  ];
  if (!run) return steps.map((s) => ({ ...s, state: 'idle' }));
  if (run.stage === 'REJECTED') return steps.map((s, i) => ({ ...s, state: i === 0 ? 'failed' : 'idle' }));
  const gh = run.github || {};
  const dep = run.deployment || {};
  const ver = run.verification || {};
  const skipped = gh.mode === 'skipped';
  const done = {
    approval: true,
    pr: Boolean(gh.pr_number) || skipped,
    merge: Boolean(gh.merged && gh.merged_content_matches) || skipped,
    deploy: Boolean(dep.patched && dep.rollout && dep.rollout.returncode === 0),
    verify: ver.result === 'PASSED',
  };
  let activeSet = false;
  return steps.map((s) => {
    let state = done[s.key] ? 'completed' : 'idle';
    if (skipped && (s.key === 'pr' || s.key === 'merge')) state = 'skipped';
    if (state === 'idle' && !activeSet && !run.final) { state = run.blocked_code ? 'warning' : 'active'; activeSet = true; }
    if (state === 'idle' && !activeSet && run.final) { state = 'failed'; activeSet = true; }
    return { ...s, state };
  });
}

/** Badge for the Kubernetes target from GET /target/kubernetes evidence. */
export function podHealth(evidence) {
  if (!evidence?.available) return { label: 'UNKNOWN', color: 'var(--text-muted)' };
  const s = evidence.summary || {};
  if (s.crash_loop) return { label: 'CRASHLOOPBACKOFF', color: 'var(--error)' };
  if (s.pods > 0 && s.ready_pods === s.pods) return { label: 'HEALTHY', color: 'var(--success)' };
  return { label: 'NOT READY', color: 'var(--warning)' };
}

/** "success" | "failure" | "pending" | "not required" -> display. */
export function ciInfo(github) {
  const status = github?.ci_status;
  if (!status) return null;
  const map = { success: ['✅', 'var(--success)'], failure: ['❌', 'var(--error)'], pending: ['⏳', 'var(--info)'] };
  const [icon, color] = map[status] || ['·', 'var(--text-muted)'];
  return { icon, color, label: status };
}

export function lastRound(run) {
  const rounds = run?.verification?.rounds || [];
  return rounds.length ? rounds[rounds.length - 1] : null;
}

export function stabilityProgress(run, nowMs = Date.now()) {
  const ver = run?.verification;
  if (!ver?.stable_since || !ver.stabilization_s) return 0;
  const since = Date.parse(ver.stable_since);
  return Math.max(0, Math.min(1, (nowMs - since) / 1000 / ver.stabilization_s));
}
