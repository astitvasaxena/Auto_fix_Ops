'use client';
// Stage 2 AI Remediation panel: confidence routing, human approval and the
// validated patch proposal. Display and requests only — the backend computes
// the route and enforces every rule. There is intentionally no apply/deploy
// control: Stage 2 never changes the running application.
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import {
  GENERATION_STEPS, MODE_INFO, STATUS_INFO, THRESHOLD, autoStartKey, canRetry, canShowDiff, canStartManually, diffLines,
  evidenceView, formatConfidence, isInProgress, needsApproval, reasonText, shouldAutoStart, stepIndex,
} from '../lib/remediation.mjs';

const H3 = { fontSize: 14, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' };
const LABEL = { fontSize: 12, color: 'var(--text-muted)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.5px' };
const LIST = { margin: 0, paddingLeft: 18, color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.6 };
const BOX = { background: 'var(--bg-secondary)', padding: 12, borderRadius: 8 };
const DIFF_COLORS = { add: 'var(--success)', del: 'var(--error)', hunk: 'var(--info)', header: 'var(--text-muted)', context: 'var(--text-secondary)' };
const STEP_ICON = { PASSED: '✅', FAILED: '❌', SKIPPED: '⏭' };
const POLL_MS = 4000;

function Section({ label, hint, children }) {
  return (
    <div style={{ marginTop: 16 }}>
      <div style={LABEL}>
        {label}
        {hint && <span style={{ textTransform: 'none', letterSpacing: 0, marginLeft: 8 }}>· {hint}</span>}
      </div>
      {children}
    </div>
  );
}

function Field({ label, children, mono = true }) {
  return (
    <div>
      <div className="text-muted text-sm">{label}</div>
      <div className={mono ? 'mono' : ''} style={{ wordBreak: 'break-all' }}>{children ?? '—'}</div>
    </div>
  );
}

function ModeBadge({ mode }) {
  const info = MODE_INFO[mode] || { label: mode, color: 'var(--text-secondary)' };
  return (
    <span className="mono" style={{ border: `1px solid ${info.color}`, color: info.color, borderRadius: 6, padding: '4px 10px', fontSize: 12, fontWeight: 700 }}>
      {info.label}
    </span>
  );
}

function ConfidenceBar({ value }) {
  const pct = typeof value === 'number' && Number.isFinite(value) ? Math.min(Math.max(value, 0), 1) * 100 : 0;
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', ...LABEL }}>
        <span>Diagnosis confidence (stored report)</span>
        <span className="mono">{formatConfidence(value)} · threshold {formatConfidence(THRESHOLD)}</span>
      </div>
      <div style={{ position: 'relative', height: 6, background: 'var(--bg-primary)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${pct}%`, borderRadius: 3, background: value >= THRESHOLD ? 'var(--success)' : 'var(--warning)' }} />
        <div style={{ position: 'absolute', top: 0, bottom: 0, left: `${THRESHOLD * 100}%`, width: 2, background: 'var(--text-primary)' }} />
      </div>
    </div>
  );
}

function Progress({ state }) {
  const current = stepIndex(state);
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
      {GENERATION_STEPS.map((step, i) => {
        const done = i < current || (i === current && state === 'COMPLETED');
        const active = i === current && state !== 'COMPLETED';
        return (
          <span key={step.key} className="text-sm" style={{
            padding: '4px 10px', borderRadius: 6, border: '1px solid var(--border-bright)',
            color: done ? 'var(--success)' : active ? 'var(--accent)' : 'var(--text-muted)',
          }}>
            {done ? '✓ ' : active ? '● ' : ''}{step.label}
          </span>
        );
      })}
    </div>
  );
}

function DiagnosisSummary({ report }) {
  const action = report?.proposed_action || {};
  return (
    <>
      <Section label="Diagnosis summary" hint="AI inference from the stored report">
        <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>{report?.incident_summary || report?.root_cause_analysis || '—'}</p>
      </Section>
      <Section label="Evidence" hint="observed in this incident">
        <ul style={LIST}>{(report?.observed_evidence || []).map((e, i) => <li key={i}>{e}</li>)}</ul>
      </Section>
      <Section label="Recommended remediation">
        <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>
          <span className="mono" style={{ fontWeight: 700, marginRight: 8 }}>{action.type || '—'}</span>
          {report?.recommended_remediation}
        </p>
      </Section>
    </>
  );
}

function ProposalReview({ proposal }) {
  const status = STATUS_INFO[proposal.effective_status] || { label: proposal.effective_status, color: 'var(--text-secondary)', text: '' };
  const source = proposal.source_report || {};
  const routingInfo = source.routing || {};
  const decision = proposal.llm_decision || {};
  const candidates = proposal.candidates || {};
  const edit = proposal.proposed_values || {};
  const steps = proposal.validation?.steps || [];
  const failed = steps.filter((s) => s.status === 'FAILED');
  const { incidentFacts, diagnosisFacts, runbooks } = evidenceView(proposal);
  const showDiff = canShowDiff(proposal);
  const reason = proposal.effective_reason_code;

  return (
    <div style={{ marginTop: 20, borderTop: '1px solid var(--border)', paddingTop: 16 }}>
      <div className="flex justify-between items-center">
        <h3 style={H3}>Patch proposal review</h3>
        <span className="mono" style={{ color: status.color, fontWeight: 700 }}>{status.label}{reason ? ` / ${reason}` : ''}</span>
      </div>
      <p className="text-sm" style={{ color: 'var(--text-secondary)', marginTop: 6 }}>{status.text}</p>
      {proposal.matches_current_diagnosis === false && (
        <p className="text-sm" style={{ color: 'var(--warning)', marginTop: 6 }}>
          ⚠ This proposal was built from an earlier version of the diagnosis.
        </p>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12, marginTop: 12 }}>
        <Field label="Proposal ID">{proposal.id}</Field>
        <Field label="Incident ID">{proposal.incident_id}</Field>
        <Field label="Diagnosis confidence">{formatConfidence(source.confidence)}</Field>
        <Field label="Routing mode">{routingInfo.mode}</Field>
        <Field label="Proposed action">{proposal.action_type}</Field>
        <Field label="LLM-selected permitted value">{decision.decision === 'PROPOSE' ? decision.new_value : decision.decision ? `${decision.decision} (${decision.decline_reason})` : null}</Field>
        <Field label="Original configuration value">{candidates.current_value}</Field>
        <Field label="Proposed configuration value">{showDiff ? edit.new_value : null}</Field>
        <Field label="Allowed menu (code-generated)">{(candidates.values || []).join(', ') || null}</Field>
        <Field label="Target">{proposal.target_ref ? `${proposal.target_file} → ${proposal.target_ref.field_path}` : proposal.target_file}</Field>
        <Field label="Generated at (UTC)">{proposal.completed_at || proposal.created_at}</Field>
        <Field label="Model">{proposal.model_info?.provider ? `${proposal.model_info.provider} ${proposal.model_info.model || ''}` : null}</Field>
      </div>

      {proposal.effective_status !== 'VALIDATED' && proposal.effective_status !== 'RUNNING' && (
        <Section label="Why no validated patch">
          <div style={{ ...BOX, borderLeft: '3px solid var(--error)' }}>
            <div className="text-sm" style={{ color: 'var(--text-primary)' }}>{reasonText(reason)}</div>
            {proposal.reason_detail && <div className="mono text-sm text-muted" style={{ marginTop: 6 }}>{proposal.reason_detail}</div>}
            {failed.map((s) => (
              <div key={s.check} className="mono text-sm" style={{ color: 'var(--error)', marginTop: 4 }}>
                ✗ {s.check}{s.code ? ` [${s.code}]` : ''} {s.detail}
              </div>
            ))}
          </div>
        </Section>
      )}

      {showDiff && (
        <Section label="Unified diff" hint={`stored proposal · sha256 ${proposal.diff_sha256?.slice(0, 12)}… · not applied`}>
          <pre className="mono" style={{ ...BOX, fontSize: 12, overflowX: 'auto', margin: 0 }}>
            {diffLines(proposal.unified_diff).map((line, i) => (
              <div key={i} style={{ color: DIFF_COLORS[line.kind], whiteSpace: 'pre' }}>{line.text}</div>
            ))}
          </pre>
        </Section>
      )}

      {proposal.rationale && (
        <Section label="Why the model selected this change" hint="AI inference — checked by code, not trusted">
          <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>{proposal.rationale}</p>
        </Section>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 16 }}>
        <Section label="Incident telemetry & manifest facts" hint="real data from this incident">
          <ul style={LIST}>
            {incidentFacts.map((f) => (
              <li key={f.id} style={{ color: f.cited ? 'var(--text-primary)' : undefined }}>
                <span className="mono">{f.id}</span> {f.text} <span className="text-muted">({f.source}{f.cited ? ', cited by the model' : ''})</span>
              </li>
            ))}
            {diagnosisFacts.map((f) => (
              <li key={f.id}><span className="mono">{f.id}</span> {f.text} <span className="text-muted">(diagnosis inference)</span></li>
            ))}
          </ul>
          {incidentFacts.length === 0 && <p className="text-sm text-muted">No facts were offered (a gate stopped the run first).</p>}
        </Section>
        <Section label="Runbook guidance retrieved" hint="general official documentation — not incident telemetry">
          {runbooks.length === 0 ? <p className="text-sm text-muted">No runbook guidance was used.</p> : runbooks.map((rb) => (
            <div key={rb.runbook_id} style={BOX}>
              <div style={{ fontWeight: 600 }}>{rb.source || rb.runbook_id}{rb.cited ? ' · cited' : ''}</div>
              <ul style={{ ...LIST, marginTop: 6 }}>{(rb.guidance || []).map((g, i) => <li key={i}>{g}</li>)}</ul>
              {rb.source_url && (
                <a href={rb.source_url} target="_blank" rel="noopener noreferrer" className="text-sm" style={{ color: 'var(--accent)' }}>{rb.source_url}</a>
              )}
            </div>
          ))}
        </Section>
      </div>

      <Section label="Validation checks" hint="performed by Python in a temporary sandbox">
        {steps.length === 0 ? <p className="text-sm text-muted">No checks ran.</p> : (
          <table className="text-sm" style={{ width: '100%', borderCollapse: 'collapse' }}>
            <tbody>
              {steps.map((s, i) => (
                <tr key={`${s.check}-${i}`} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '4px 8px', width: 28 }}>{STEP_ICON[s.status] || '•'}</td>
                  <td className="mono" style={{ padding: '4px 8px' }}>{s.check}</td>
                  <td className="mono" style={{ padding: '4px 8px', color: s.status === 'FAILED' ? 'var(--error)' : 'var(--text-muted)' }}>{s.code || ''}</td>
                  <td style={{ padding: '4px 8px', color: 'var(--text-secondary)' }}>{s.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      {(proposal.review_notes || []).length > 0 && (
        <Section label="Review notes">
          <ul style={LIST}>{proposal.review_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </Section>
      )}
    </div>
  );
}

export default function AIRemediationPanel({ incidentId, report, refreshKey }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const requested = useRef(new Set());

  const load = useCallback(() => {
    api.getPatchProposals(incidentId)
      .then((v) => { setView(v); setError(null); })
      .catch((e) => setError(e.message));
  }, [incidentId]);

  useEffect(() => { load(); }, [load, refreshKey]);

  // Poll while a generation is queued or running (WebSocket events also trigger reloads).
  useEffect(() => {
    if (!isInProgress(view)) return undefined;
    const t = setTimeout(load, POLL_MS);
    return () => clearTimeout(t);
  }, [view, load]);

  // Automatic route: ask the backend once per incident + diagnosis version. The backend's
  // atomic claim makes refreshes and other tabs no-ops as well.
  useEffect(() => {
    if (!shouldAutoStart(view, requested.current)) return;
    requested.current.add(autoStartKey(view));
    api.startPatchProposal(incidentId)
      .then((res) => setView(res.view))
      .catch((e) => setError(e.message));
  }, [view, incidentId]);

  const act = async (fn) => {
    setBusy(true);
    try {
      const res = await fn();
      setView(res.view);
      setError(null);
    } catch (e) {
      setError(e.message);
      load();
    }
    setBusy(false);
  };

  if (!view && !error) {
    return <div className="card mb-16"><h3 style={H3}>🛠 AI Remediation</h3><span className="loading-spinner" /></div>;
  }

  const routing = view?.routing;
  const proposal = view?.latest_proposal;
  const lowConfidence = routing && routing.route === 'HUMAN_APPROVAL_REQUIRED';

  return (
    <div className="card mb-16" style={{ borderColor: routing ? (MODE_INFO[routing.routing_mode]?.color || 'var(--border)') : 'var(--border)' }}>
      <div className="flex justify-between items-center" style={{ gap: 12, flexWrap: 'wrap' }}>
        <h3 style={H3}>🛠 AI Remediation <span style={{ textTransform: 'none', letterSpacing: 0 }}>· Stage 2 — proposal only</span></h3>
        {routing && <ModeBadge mode={routing.routing_mode} />}
      </div>

      {error && <p className="text-sm" style={{ color: 'var(--error)', marginTop: 8 }}>⚠ {error}</p>}

      {!routing && view?.routing_error && (
        <p className="text-sm text-muted" style={{ marginTop: 8 }}>
          Not eligible for an AI patch proposal: <span className="mono">{view.routing_error.code}</span> — {view.routing_error.detail}
        </p>
      )}

      {routing && (
        <>
          <div style={{ marginTop: 12 }}><ConfidenceBar value={routing.diagnosis_confidence} /></div>
          <p className="text-sm" style={{ color: 'var(--text-secondary)', marginTop: 8 }}>
            {lowConfidence
              ? `Confidence ${formatConfidence(routing.diagnosis_confidence)} is below ${formatConfidence(THRESHOLD)}, so the backend requires an explicit human approval before the AI is asked to generate a patch.`
              : `Confidence ${formatConfidence(routing.diagnosis_confidence)} meets the ${formatConfidence(THRESHOLD)} threshold, so no human approval is needed: the backend selected the automatic AI route.`}
            {' '}Either way, the patch must pass every safety check, and nothing is applied.
          </p>

          <DiagnosisSummary report={report} />

          {needsApproval(view) && (
            <div style={{ ...BOX, marginTop: 16, border: '1px solid var(--warning)' }}>
              <div style={{ color: 'var(--warning)', fontWeight: 700 }}>⚠ Human approval required</div>
              <p className="text-sm text-muted" style={{ marginTop: 4 }}>
                Approving lets the AI choose a value from a code-generated menu and lets Python build and validate the patch.
                It does not apply or deploy anything. Rejecting means no patch is generated for this diagnosis.
              </p>
              <div className="form-group" style={{ marginTop: 8 }}>
                <textarea rows={2} maxLength={500} placeholder="Optional note (recorded with your decision)"
                  value={note} onChange={(e) => setNote(e.target.value)} />
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button className="btn btn-success" disabled={busy} onClick={() => act(() => api.approvePatchGeneration(incidentId, note))}>
                  {busy ? <span className="loading-spinner" /> : '✓ Approve AI Patch Generation'}
                </button>
                <button className="btn btn-danger" disabled={busy} onClick={() => act(() => api.rejectPatchGeneration(incidentId, note))}>
                  ✗ Reject Recommendation
                </button>
              </div>
              <p className="text-sm text-muted" style={{ marginTop: 8 }}>
                Local demo only: this API has no sign-in, so the decision is recorded as unauthenticated with your
                browser&apos;s client details. Do not expose it publicly without authentication and authorization.
              </p>
            </div>
          )}

          {routing.decision === 'APPROVED' || routing.decision === 'REJECTED' ? (
            <Section label="Human decision">
              <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>
                {routing.decision} at {routing.decided_at} by{' '}
                {routing.decided_by?.authenticated ? routing.decided_by.identity : `an unauthenticated ${routing.decided_by?.source || 'client'}${routing.decided_by?.client_host ? ` (${routing.decided_by.client_host})` : ''}`}
                {routing.decision_reason ? ` — “${routing.decision_reason}”` : ''}
                {routing.decision === 'REJECTED' ? '. No patch will be generated for this diagnosis.' : ''}
              </p>
            </Section>
          ) : null}

          {routing.decision !== 'PENDING' && routing.decision !== 'REJECTED' && (
            <Section label="Patch generation progress">
              <Progress state={routing.generation_state} />
              {canStartManually(view) && !view.auto_generation_enabled && (
                <div style={{ marginTop: 8 }}>
                  <p className="text-sm text-muted">Automatic generation is turned off (PATCH_PROPOSALS_AUTO=false).</p>
                  <button className="btn btn-outline" style={{ marginTop: 6 }} disabled={busy}
                    onClick={() => act(() => api.startPatchProposal(incidentId))}>
                    ▶ Generate AI patch proposal
                  </button>
                </div>
              )}
              {canRetry(view) && (
                <button className="btn btn-outline" style={{ marginTop: 8 }} disabled={busy}
                  onClick={() => act(() => api.startPatchProposal(incidentId, true))}>
                  ↻ Retry generation
                </button>
              )}
            </Section>
          )}
        </>
      )}

      {proposal ? <ProposalReview proposal={proposal} /> : routing && (
        <p className="text-sm text-muted" style={{ marginTop: 16 }}>No patch proposal has been stored for this incident yet.</p>
      )}

      <div style={{ ...BOX, marginTop: 16 }}>
        <div className="text-sm" style={{ fontWeight: 600 }}>Next: Stages 3-4 (approval, pull request, deployment)</div>
        <p className="text-sm text-muted" style={{ marginTop: 4 }}>
          Stage 2 stops at a validated proposal: nothing here applies, commits, pushes or deploys it. A human can
          approve or reject the exact validated diff in the panel below; deployment then needs the PR merged
          (unless configured otherwise) and an explicitly enabled, allowlisted target.
        </p>
      </div>
    </div>
  );
}
