'use client';
// Stages 3-4 panel: human approval of the validated proposal, GitHub pull request,
// controlled deployment, recovery verification and the incident-to-recovery
// timeline. Every state shown here comes from GET /incidents/{id}/remediation;
// the buttons only send a decision (and the proposal id the human reviewed).
import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';
import { RUN_STAGE_INFO, blockedText, lastRound, runSteps, stabilityProgress } from '../lib/pipeline.mjs';

const H3 = { fontSize: 14, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' };
const LABEL = { fontSize: 12, color: 'var(--text-muted)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.5px' };
const BOX = { background: 'var(--bg-secondary)', padding: 12, borderRadius: 8 };
const STEP_ICON = { completed: '✅', active: '⏳', warning: '⚠️', failed: '❌', skipped: '⏭', idle: '○' };
const STEP_COLOR = { completed: 'var(--success)', active: 'var(--info)', warning: 'var(--warning)', failed: 'var(--error)', skipped: 'var(--text-muted)', idle: 'var(--text-muted)' };
const TOKEN_KEY = 'autofixops.approverToken';
const POLL_MS = 4000;

function Section({ label, children }) {
  return (
    <div style={{ marginTop: 16 }}>
      <div style={LABEL}>{label}</div>
      {children}
    </div>
  );
}

function readToken() {
  try { return window.sessionStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

function saveToken(value) {
  try { window.sessionStorage.setItem(TOKEN_KEY, value); } catch { /* private mode: keep in memory only */ }
}

function Steps({ run }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8 }}>
      {runSteps(run).map((s) => (
        <div key={s.key} style={{ ...BOX, borderLeft: `3px solid ${STEP_COLOR[s.state]}` }}>
          <div className="text-sm">{STEP_ICON[s.state]} {s.label}</div>
          <div className="text-muted" style={{ fontSize: 11, marginTop: 2 }}>{s.state}</div>
        </div>
      ))}
    </div>
  );
}

function GitHubInfo({ github }) {
  if (!github) return <p className="text-sm text-muted">No pull request yet.</p>;
  if (github.mode === 'skipped') {
    return <p className="text-sm text-muted">Pull request step skipped by configuration ({github.reason}). No PR was created.</p>;
  }
  return (
    <div className="text-sm" style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
      <div><div className="text-muted">Pull request</div>
        <a href={github.pr_url} target="_blank" rel="noreferrer" className="mono">#{github.pr_number}</a>
        {github.draft && <span className="text-muted"> (draft)</span>}</div>
      <div><div className="text-muted">State</div>
        <span className="mono">{github.merged ? 'merged' : github.pr_state}</span>
        {github.merged && <span className="text-muted"> · content {github.merged_content_matches ? 'matches' : 'DIFFERS'}</span>}</div>
      <div><div className="text-muted">Branch</div><span className="mono" style={{ wordBreak: 'break-all' }}>{github.branch}</span></div>
    </div>
  );
}

function DeploymentInfo({ deployment, rollback }) {
  if (!deployment) return <p className="text-sm text-muted">Not deployed.</p>;
  return (
    <div className="text-sm">
      <div className="mono">{deployment.target} · {deployment.container} · {deployment.resource}:
        {' '}{deployment.from} → {deployment.to}</div>
      <div className="text-muted" style={{ marginTop: 4 }}>
        kubectl context <span className="mono">{deployment.context}</span>
        {deployment.started_at && <> · started {new Date(deployment.started_at).toLocaleTimeString()}</>}
        {deployment.finished_at && <> · rolled out {new Date(deployment.finished_at).toLocaleTimeString()}</>}
      </div>
      {deployment.error && <div style={{ color: 'var(--error)', marginTop: 4 }}>{deployment.error}: {deployment.detail}</div>}
      {rollback && (
        <div style={{ color: rollback.ok ? 'var(--warning)' : 'var(--error)', marginTop: 4 }}>
          {rollback.ok ? `Rolled back to ${deployment.from}` : 'ROLLBACK FAILED — manual action required'}: {rollback.reason}
          {rollback.git_revert?.url && <> · <a href={rollback.git_revert.url} target="_blank" rel="noreferrer">revert PR</a></>}
        </div>
      )}
    </div>
  );
}

function VerificationInfo({ run }) {
  const ver = run?.verification;
  if (!ver) return <p className="text-sm text-muted">Verification starts after a successful rollout.</p>;
  const round = lastRound(run);
  const progress = stabilityProgress(run);
  return (
    <div className="text-sm">
      <div className="text-muted">
        {ver.round_count || 0} round(s) · stable for {Math.round(progress * ver.stabilization_s)}s of {ver.stabilization_s}s
        · timeout {ver.timeout_s}s {ver.result && <>· result <strong>{ver.result}</strong></>}
      </div>
      <div style={{ height: 6, background: 'var(--bg-primary)', borderRadius: 3, overflow: 'hidden', margin: '6px 0' }}>
        <div style={{ height: '100%', width: `${progress * 100}%`, background: 'var(--success)' }} />
      </div>
      {round && (
        <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>
          {round.checks.map((c) => (
            <li key={c.name}>{c.ok ? '✅' : '❌'} <span className="mono">{c.name}</span> — {c.detail}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Timeline({ items }) {
  if (!items?.length) return null;
  return (
    <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.7, color: 'var(--text-secondary)' }}>
      {items.map((t, i) => (
        <li key={`${t.at}-${i}`}>
          <span className="mono text-muted">{new Date(t.at).toLocaleTimeString()}</span> {t.label}
          {t.note && <span className="text-muted"> — {t.note}</span>}
        </li>
      ))}
    </ol>
  );
}

export default function RemediationPanel({ incidentId, refreshKey, onChange }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [reason, setReason] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.getRemediation(incidentId).then((v) => { setView(v); setError(null); onChange?.(v); })
      .catch((e) => setError(e.message));
  }, [incidentId, onChange]);

  useEffect(() => { setToken(readToken()); }, []);
  useEffect(() => { load(); }, [load, refreshKey]);
  useEffect(() => {
    if (view?.run?.final || (!view?.run && !view?.can_decide)) return undefined;
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [view?.run?.final, view?.run, view?.can_decide, load]);

  const act = async (fn) => {
    setBusy(true);
    try { await fn(); setReason(''); setError(null); } catch (e) { setError(e.message); }
    setBusy(false);
    load();
  };

  if (!view) return error ? <div className="card mb-16"><p className="text-sm" style={{ color: 'var(--error)' }}>{error}</p></div> : null;
  const { run, proposal, config } = view;
  const stageInfo = run ? RUN_STAGE_INFO[run.stage] || { label: run.stage, color: 'var(--text-secondary)' } : null;
  const needsToken = view.approver_auth === 'token';
  const values = proposal?.proposed_values;

  return (
    <div className="card mb-16">
      <div className="flex justify-between items-center">
        <h3 style={H3}>Approval, Pull Request, Deployment &amp; Verification</h3>
        {stageInfo && (
          <span className="mono" style={{ border: `1px solid ${stageInfo.color}`, color: stageInfo.color, borderRadius: 6, padding: '4px 10px', fontSize: 12, fontWeight: 700 }}>
            {stageInfo.label}
          </span>
        )}
      </div>

      <div style={{ marginTop: 12 }}><Steps run={run} /></div>

      {run?.blocked_code && (
        <div style={{ ...BOX, marginTop: 12, borderLeft: '3px solid var(--warning)' }}>
          <div className="text-sm" style={{ fontWeight: 600 }}>Waiting: {run.blocked_code}</div>
          <p className="text-sm text-muted" style={{ marginTop: 4 }}>{blockedText(run.blocked_code, run.blocked_detail)}</p>
          {run.blocked_detail && <p className="mono text-muted" style={{ fontSize: 11, marginTop: 4 }}>{run.blocked_detail}</p>}
        </div>
      )}

      {(view.can_decide || (run && !run.final) || view.can_close) && needsToken && (
        <Section label="Approver token">
          <input type="password" className="input" value={token} placeholder="token from APPROVER_TOKENS"
            onChange={(e) => { setToken(e.target.value); saveToken(e.target.value); }} style={{ width: '100%' }} />
        </Section>
      )}

      {view.can_decide && (
        <Section label="Human approval required">
          <p className="text-sm text-muted">
            Approving lets AutoFixOps open a pull request with exactly the validated diff shown above
            {values && <> (<span className="mono">{values.old_value} → {values.new_value}</span>)</>}. Deployment
            happens only after that PR is merged unchanged (or, if configured, without a PR) and only to an allowlisted target.
            {view.approver_auth === 'local-only' && ' No approver authentication is configured: this approval is recorded as an unauthenticated local decision.'}
          </p>
          <textarea className="input" rows={2} value={reason} maxLength={500} placeholder="Reason (required to reject)"
            onChange={(e) => setReason(e.target.value)} style={{ width: '100%', marginTop: 8 }} />
          <div className="flex" style={{ gap: 8, marginTop: 8 }}>
            <button className="btn btn-success" disabled={busy}
              onClick={() => act(() => api.approveRemediation(incidentId, proposal.id, reason, token))}>✓ Approve remediation</button>
            <button className="btn btn-outline" disabled={busy || !reason.trim()}
              onClick={() => act(() => api.rejectRemediation(incidentId, proposal.id, reason, token))}>✕ Reject</button>
          </div>
        </Section>
      )}

      {!view.can_decide && !run && (
        <p className="text-sm text-muted" style={{ marginTop: 12 }}>
          {proposal?.effective_status === 'VALIDATED'
            ? 'The validated proposal no longer matches this incident’s diagnosis or status, so it cannot be approved.'
            : 'Remediation needs a VALIDATED patch proposal first (see the AI Remediation panel above).'}
        </p>
      )}

      {run && (
        <>
          <Section label="Decision">
            <p className="text-sm">
              <strong>{run.stage === 'REJECTED' ? 'Rejected' : 'Approved'}</strong>
              {' '}at {run.decided_at && new Date(run.decided_at).toLocaleString()} by{' '}
              {run.decided_by?.authenticated ? <span className="mono">{run.decided_by.identity}</span>
                : <span className="text-muted">an unauthenticated local user ({run.decided_by?.client_host || 'unknown host'})</span>}
              {run.decision_reason && <> — “{run.decision_reason}”</>}
            </p>
          </Section>
          <Section label="GitHub pull request"><GitHubInfo github={run.github} /></Section>
          <Section label="Deployment"><DeploymentInfo deployment={run.deployment} rollback={run.rollback} /></Section>
          <Section label="Recovery verification"><VerificationInfo run={run} /></Section>
          {!run.final && (
            <button className="btn btn-outline" style={{ marginTop: 12 }} disabled={busy || run.step_in_progress}
              onClick={() => act(() => api.advanceRemediation(incidentId, token))}>
              ↻ {run.blocked_code ? 'Retry this step' : 'Check now'}
            </button>
          )}
        </>
      )}

      {view.can_close && (
        <Section label="Close without automated remediation">
          <div className="flex" style={{ gap: 8 }}>
            <input className="input" value={reason} maxLength={500} placeholder="Reason (required)"
              onChange={(e) => setReason(e.target.value)} style={{ flex: 1 }} />
            <button className="btn btn-outline" disabled={busy || !reason.trim()}
              onClick={() => act(() => api.closeIncident(incidentId, reason, token))}>Close incident</button>
          </div>
        </Section>
      )}

      <Section label="Configuration (no secrets)">
        <p className="text-sm text-muted">
          PR required: <span className="mono">{String(config.require_pr)}</span> ·
          deploy: <span className="mono">{config.deploy_ready ? `kubectl @ ${config.kube_context}` : config.deploy_mode}</span> ·
          verification: <span className="mono">{config.verify_ready ? `${config.health_url}, ${config.stabilization_s}s window` : 'not configured'}</span> ·
          approvals: <span className="mono">{view.approver_auth}</span>
        </p>
      </Section>

      <Section label="Incident timeline"><Timeline items={view.timeline} /></Section>
      {error && <p className="text-sm" style={{ color: 'var(--error)', marginTop: 8 }}>{error}</p>}
    </div>
  );
}
