// Pure view logic for the Stage 2 AI Remediation panel (no React, no fetch),
// so it can be unit-tested with `node --test`. The backend decides the route;
// these helpers only decide what to display and when to ask the backend.

export const THRESHOLD = 0.8;

export const MODE_INFO = {
  AUTOMATIC_AI: { label: 'AUTOMATIC AI REMEDIATION', color: 'var(--success)' },
  HUMAN_APPROVAL_REQUIRED: { label: 'HUMAN APPROVAL REQUIRED', color: 'var(--warning)' },
  HUMAN_APPROVED: { label: 'HUMAN APPROVED', color: 'var(--info)' },
  HUMAN_REJECTED: { label: 'HUMAN REJECTED', color: 'var(--error)' },
};

export const STATUS_INFO = {
  RUNNING: { label: 'RUNNING', color: 'var(--info)', text: 'The proposer is running: the local model is choosing a value and the patch is being validated.' },
  VALIDATED: { label: 'VALIDATED', color: 'var(--success)', text: 'Every safety check passed. The diff below is the stored, validated proposal.' },
  NOT_GENERATED: { label: 'NOT GENERATED', color: 'var(--warning)', text: 'A gate stopped generation before a patch was produced.' },
  REJECTED: { label: 'REJECTED', color: 'var(--error)', text: 'A patch candidate was produced but failed validation, so no diff is kept.' },
  FAILED: { label: 'FAILED', color: 'var(--error)', text: 'The run did not complete (model, deadline or database problem). Nothing was stored as a patch.' },
};

export const GENERATION_STEPS = [
  { key: 'ROUTED', label: 'Route decided' },
  { key: 'QUEUED', label: 'Generation queued' },
  { key: 'RUNNING', label: 'Model choice + validation' },
  { key: 'COMPLETED', label: 'Result stored' },
];

const REASON_TEXT = {
  LOW_CONFIDENCE: 'Diagnosis confidence is below the threshold and no human approval is recorded.',
  SYSTEM_DISABLED: 'The kill switch is DISABLED.',
  INCIDENT_CHANGED: 'The incident or its diagnosis changed while the proposal was generated.',
  APPROVAL_INVALID: 'The recorded approval no longer matches this diagnosis.',
  INSUFFICIENT_EVIDENCE: 'Telemetry does not show the resource near its limit.',
  NO_SUPPORTING_RUNBOOK: 'The official runbook for this action was not retrieved for this incident.',
  UNSUPPORTED_ACTION: 'Only memory/CPU limit increases can be proposed in Stage 2.',
  INCONSISTENT_DIAGNOSIS: 'The proposed action conflicts with the diagnosis.',
  REPORT_INVALID: 'The stored diagnosis report is malformed.',
  UNSUPPORTED_MANIFEST: 'The target manifest has a shape the proposer refuses to edit.',
  MODEL_DECLINED: 'The model declined to propose a change.',
  LLM_UNAVAILABLE: 'The local model could not be reached.',
  LLM_TIMEOUT: 'The local model did not answer in time.',
  LLM_INVALID_CHOICE: 'The model chose a value outside the allowed menu.',
  LLM_MALFORMED_OUTPUT: 'The model answer did not match the required format.',
  UNGROUNDED_CLAIM: 'The model rationale used facts that were not provided.',
  BOUNDS_VIOLATION: 'The value is outside the allowed bounds.',
  DEADLINE_EXCEEDED: 'Validation exceeded its deadline.',
  PERSISTENCE_FAILED: 'The result could not be stored.',
  ABANDONED: 'The run stopped without recording a result.',
  INTEGRITY_MISMATCH: 'The stored record failed its integrity check.',
  INTERRUPTED: 'The run was interrupted.',
  AT_BOUND: 'The limit is already at the maximum allowed value.',
  REPO_MISMATCH: 'The configured repository does not match this checkout.',
  TARGET_MISMATCH: 'The alert target does not match the manifest.',
};

export function reasonText(code) {
  return code ? (REASON_TEXT[code] || code.replace(/_/g, ' ').toLowerCase()) : '';
}

// Truncates (never rounds up), so 0.79999 is not shown as "80.00%" next to "approval required".
// The epsilon absorbs float error (0.7999 * 10000 === 7998.999...).
export function formatConfidence(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${(Math.floor(value * 10000 + 1e-6) / 100).toFixed(2)}%`;
}

export function autoStartKey(view) {
  const r = view?.routing;
  return r ? `${view.incident_id}:${r.report_sha256}` : null;
}

// True only when the backend says the automatic route applies and nothing was requested yet
// for this diagnosis version. The backend enforces the same rule; this only avoids useless calls.
export function shouldAutoStart(view, requestedKeys) {
  if (!canStartManually(view) || view.auto_generation_enabled !== true) return false;
  const key = autoStartKey(view);
  return !(requestedKeys && requestedKeys.has(key));
}

// Automatic route, nothing requested yet. With PATCH_PROPOSALS_AUTO=false the page offers a
// button instead of starting on its own.
export function canStartManually(view) {
  const r = view?.routing;
  return Boolean(r && view.incident_status === 'DIAGNOSED' && r.route === 'AUTOMATIC_AI'
    && r.decision === 'AUTO' && r.generation_state === 'NOT_REQUESTED');
}

export function needsApproval(view) {
  const r = view?.routing;
  return Boolean(r && view.incident_status === 'DIAGNOSED' && r.route === 'HUMAN_APPROVAL_REQUIRED' && r.decision === 'PENDING');
}

export function isInProgress(view) {
  const state = view?.routing?.generation_state;
  return state === 'QUEUED' || state === 'RUNNING' || view?.latest_proposal?.effective_status === 'RUNNING';
}

// The backend computes retry_allowed (FAILED run, transient refusal, or a queued job that went stale);
// the page only shows the button. The backend re-checks on the request.
export function canRetry(view) {
  return Boolean(view?.routing?.retry_allowed === true && view.incident_status === 'DIAGNOSED');
}

export function stepIndex(state) {
  return { NOT_REQUESTED: 0, QUEUED: 1, RUNNING: 2, COMPLETED: 3 }[state] ?? 0;
}

// A diff is shown only for a stored, intact VALIDATED proposal (the backend nulls it otherwise).
export function canShowDiff(proposal) {
  return Boolean(proposal && proposal.effective_status === 'VALIDATED'
    && typeof proposal.unified_diff === 'string' && proposal.unified_diff.length > 0);
}

export function diffLines(diff) {
  if (typeof diff !== 'string') return [];
  return diff.replace(/\n$/, '').split('\n').map((text) => ({
    text,
    kind: text.startsWith('+++') || text.startsWith('---') ? 'header'
      : text.startsWith('@@') ? 'hunk'
        : text.startsWith('+') ? 'add'
          : text.startsWith('-') ? 'del' : 'context',
  }));
}

// Splits what the model was given into incident telemetry (code-derived facts) vs general runbook guidance.
export function evidenceView(proposal) {
  const evidence = proposal?.evidence || {};
  const cited = new Set(evidence.fact_ids_cited || []);
  const facts = (evidence.facts_offered || []).map((f) => ({ ...f, cited: cited.has(f.id) }));
  return {
    incidentFacts: facts.filter((f) => f.source !== 'diagnosis'),
    diagnosisFacts: facts.filter((f) => f.source === 'diagnosis'),
    runbooks: (evidence.runbooks_offered || []).map((rb) => ({
      ...rb, cited: (evidence.runbook_ids_cited || []).includes(rb.runbook_id),
    })),
  };
}
