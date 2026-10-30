'use client';
import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { api } from '../../../lib/api';
import { formatConfidence } from '../../../lib/remediation.mjs';
import AIRemediationPanel from '../../../components/AIRemediationPanel';

const PIPELINE_STAGES = [
  { key: 'INGESTED', label: 'Ingest', icon: '📥' },
  { key: 'CONTEXT_BUILT', label: 'Context', icon: '🔍' },
  { key: 'DIAGNOSED', label: 'Diagnosis', icon: '🧠' },
  { key: 'POLICY_APPROVED', label: 'Policy', icon: '🛡' },
  { key: 'PENDING_PR_MERGE', label: 'PR', icon: '🔧' },
  { key: 'VERIFIED', label: 'Verify', icon: '✅' },
  { key: 'RESOLVED', label: 'Resolved', icon: '🎯' },
];

const STATUS_ORDER = {
  INGESTED: 0, CONTEXT_BUILT: 1, DIAGNOSED: 2, POLICY_APPROVED: 3,
  REMEDIATING: 3.5, PENDING_PR_MERGE: 4, VERIFIED: 5, RESOLVED: 6,
  FAILED: -1, ESCALATED: -2,
};

function PipelineFlow({ status }) {
  const currentIdx = STATUS_ORDER[status] ?? -1;
  const isFailed = status === 'FAILED';
  const isEscalated = status === 'ESCALATED';

  return (
    <div className="pipeline-flow">
      {PIPELINE_STAGES.map((stage, i) => {
        let nodeClass = '';
        let labelClass = '';
        if (isFailed || isEscalated) {
          nodeClass = i <= Math.abs(currentIdx) ? 'failed' : '';
          labelClass = i <= Math.abs(currentIdx) ? 'failed' : '';
        } else if (i < currentIdx) {
          nodeClass = 'completed';
          labelClass = 'completed';
        } else if (i === Math.floor(currentIdx)) {
          nodeClass = 'active';
          labelClass = 'active';
        }

        return (
          <div key={stage.key} style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && <div className={`pipeline-connector ${i <= currentIdx ? 'completed' : ''}`} />}
            <div className="pipeline-step">
              <div className={`pipeline-node ${nodeClass}`}>{stage.icon}</div>
              <div className={`pipeline-label ${labelClass}`}>{stage.label}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

const PROVIDER_LABELS = { ollama: 'Ollama', openai: 'OpenAI' };
// Older stored reports cite the retired synthetic runbooks; label them as such.
const SOURCE_TYPE_LABELS = { official_documentation: 'Official documentation', synthetic_runbook: 'Synthetic runbook (retired)' };
const RISK_COLORS = { LOW: 'var(--success)', MEDIUM: 'var(--warning)', HIGH: 'var(--error)' };
const SECTION_LABEL = { fontSize: 12, color: 'var(--text-muted)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.5px' };
const LIST_STYLE = { margin: 0, paddingLeft: 18, color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.6 };

// "qwen2.5:3b" + "ollama" → "Qwen 2.5 3B (Ollama)"; unknown formats are shown as-is.
function analystLabel(model, provider) {
  const m = /^([a-z]+)([\d.]*)(?::(\d+(?:\.\d+)?)b)?$/i.exec(model || '');
  const name = m
    ? [m[1][0].toUpperCase() + m[1].slice(1), m[2], m[3] && `${m[3]}B`].filter(Boolean).join(' ')
    : (model || 'unknown model');
  return `${name} (${PROVIDER_LABELS[provider] || provider || 'unknown provider'})`;
}

function AIReportSection({ label, hint, children }) {
  return (
    <div style={{ marginTop: 16 }}>
      <div style={SECTION_LABEL}>
        {label}
        {hint && <span style={{ textTransform: 'none', letterSpacing: 0, marginLeft: 8 }}>· {hint}</span>}
      </div>
      {children}
    </div>
  );
}

function AIReportCard({ report }) {
  const action = report.proposed_action || {};
  const risk = report.risk_impact || {};
  const runbooks = report.retrieved_runbook_evidence || [];
  const confidence = report.confidence ?? 0;

  return (
    <div className="card mb-16">
      <div className="flex justify-between items-center" style={{ marginBottom: 12 }}>
        <h3 style={{ fontSize: 14, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
          🤖 AI Incident Analysis
        </h3>
        <span className="text-sm text-muted">AI Analyst: {analystLabel(report.model, report.llm_provider)}</span>
      </div>

      {report.incident_summary && (
        <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>{report.incident_summary}</p>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 16 }}>
        <div>
          <div style={SECTION_LABEL}>Incident Type</div>
          <span className="mono" style={{ fontSize: 16, fontWeight: 700 }}>{report.incident_type || '—'}</span>
        </div>
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', ...SECTION_LABEL }}>
            <span>Confidence</span>
            <span className="mono">{formatConfidence(confidence)}</span>
          </div>
          <div style={{ height: 6, background: 'var(--bg-primary)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{
              height: '100%', width: `${confidence * 100}%`, borderRadius: 3,
              background: confidence >= 0.8 ? 'var(--success)' : 'var(--warning)',
            }} />
          </div>
        </div>
      </div>

      <AIReportSection label="Root Cause Analysis" hint="AI inference">
        <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>{report.root_cause_analysis}</p>
      </AIReportSection>

      <AIReportSection label="Observed Evidence" hint="from this incident">
        <ul style={LIST_STYLE}>
          {(report.observed_evidence || []).map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      </AIReportSection>

      <AIReportSection label="RAG / Operational Guidance" hint="curated from official documentation — not past incidents">
        {runbooks.length === 0 ? (
          <p className="text-sm text-muted">
            No relevant guidance was found{report.rag?.status ? ` (${report.rag.status})` : ''}.
          </p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {runbooks.map((rb) => (
              <div key={rb.runbook_id} style={{ background: 'var(--bg-secondary)', padding: 12, borderRadius: 8 }}>
                <div className="flex justify-between items-center">
                  <span style={{ fontWeight: 600 }}>
                    {rb.source_organization ? `${rb.source_organization} — ${rb.source_title}` : rb.title}
                  </span>
                  <span className="mono text-sm text-muted">Similarity {rb.similarity_score?.toFixed(2)}</span>
                </div>
                <div className="text-sm text-muted" style={{ marginTop: 4 }}>
                  Source: {SOURCE_TYPE_LABELS[rb.source_type] || rb.source_type} · Status:{' '}
                  {rb.source_status ? rb.source_status[0].toUpperCase() + rb.source_status.slice(1) : '—'} /{' '}
                  {rb.verified ? 'verified' : 'unverified'}
                </div>
                <div className="text-sm" style={{ color: 'var(--text-secondary)', marginTop: 8 }}>
                  <span className="text-muted">Guidance used: </span>{rb.guidance_used}
                </div>
                {rb.source_url && (
                  <div className="text-sm" style={{ marginTop: 6 }}>
                    <span className="text-muted">Source: </span>
                    <a href={rb.source_url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)' }}>
                      {rb.source_url}
                    </a>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </AIReportSection>

      <AIReportSection label="Reasoning" hint="AI inference">
        <ul style={LIST_STYLE}>
          {(report.reasoning || []).map((r, i) => <li key={i}>{r}</li>)}
        </ul>
      </AIReportSection>

      <AIReportSection label="Recommended Remediation">
        <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>{report.recommended_remediation}</p>
      </AIReportSection>

      <AIReportSection label="Risk / Impact">
        <p className="text-sm" style={{ color: 'var(--text-secondary)' }}>
          <span className="mono" style={{ color: RISK_COLORS[risk.level] || 'var(--text-secondary)', fontWeight: 700, marginRight: 8 }}>
            {risk.level || '—'}
          </span>
          {risk.description}
        </p>
      </AIReportSection>

      <AIReportSection label="Proposed Action">
        <div style={{ border: '1px solid var(--warning)', borderRadius: 8, padding: 12 }}>
          <div className="flex justify-between items-center">
            <span className="mono" style={{ fontWeight: 700 }}>{action.type}</span>
            <span className="text-sm" style={{ color: 'var(--warning)' }}>
              {report.proposed_action_executed ? 'Executed' : 'Proposed action only — not executed'}
            </span>
          </div>
          {action.target && <div className="text-sm text-muted" style={{ marginTop: 4 }}>Target: <span className="mono">{action.target}</span></div>}
          {action.rationale && <p className="text-sm" style={{ color: 'var(--text-secondary)', marginTop: 4 }}>{action.rationale}</p>}
        </div>
      </AIReportSection>
    </div>
  );
}

export default function IncidentDetailPage() {
  const { id } = useParams();
  const router = useRouter();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [approving, setApproving] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);
  const [toast, setToast] = useState(null);
  const [eventCount, setEventCount] = useState(0);

  useEffect(() => {
    const load = () => {
      api.getIncidentContext(id)
        .then(setData)
        .catch(() => {})
        .finally(() => setLoading(false));
    };
    load();
    const cleanup = api.listenEvents((event) => {
      if (event.incident_id === id) {
        load();
        setEventCount((n) => n + 1);
      }
    });
    return cleanup;
  }, [id]);

  const handleApprove = async () => {
    setApproving(true);
    try {
      await api.approveEscalation(id);
      setToast({ type: 'success', msg: 'Escalation approved — remediation dispatched' });
      setTimeout(() => window.location.reload(), 1500);
    } catch (e) {
      setToast({ type: 'error', msg: e.message });
    }
    setApproving(false);
  };

  const handleRollback = async () => {
    setRollingBack(true);
    try {
      const result = await api.rollbackIncident(id);
      setToast({ type: 'success', msg: result.message || `Rollback queued: ${result.pr_url}` });
    } catch (e) {
      setToast({ type: 'error', msg: e.message });
    }
    setRollingBack(false);
  };

  if (loading) return <div className="page"><div className="empty-state"><span className="loading-spinner" /></div></div>;
  if (!data) return <div className="page"><div className="empty-state"><div className="icon">❌</div><p>Incident not found</p></div></div>;

  const { incident, telemetry_context, remediation_audits } = data;
  const audit = remediation_audits?.[0] || {};
  const resolved = incident.resolved_target;
  const policyDecision = incident.policy_decision;

  return (
    <div className="page">
      <div className="page-header flex justify-between items-center">
        <div>
          <button className="btn btn-outline" onClick={() => router.push('/incidents')} style={{ marginBottom: 12 }}>
            ← Back
          </button>
          <h2>{incident.alert_name}</h2>
          <p className="mono text-muted">{incident.id}</p>
        </div>
        <span className={`badge ${incident.status.toLowerCase()}`}>
          {incident.status.replace(/_/g, ' ')}
        </span>
      </div>

      {/* Pipeline Flow */}
      <div className="card mb-16">
        <PipelineFlow status={incident.status} />
      </div>

      {/* AI Incident Analysis (stored ai_report; display only) */}
      {incident.ai_report && <AIReportCard report={incident.ai_report} />}

      {/* Stage 2: confidence-routed AI patch proposal (proposal only, never applied) */}
      {incident.ai_report && (
        <AIRemediationPanel incidentId={incident.id} report={incident.ai_report} refreshKey={eventCount} />
      )}

      {/* Main Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {/* Diagnosis */}
        <div className="card">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Diagnosis
          </h3>
          <div style={{ marginBottom: 12 }}>
            <span className="mono" style={{ fontSize: 18, fontWeight: 700 }}>
              {incident.diagnosis || 'Pending'}
            </span>
          </div>
          {incident.confidence != null && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                <span>Confidence</span>
                <span className="mono">{formatConfidence(incident.confidence)}</span>
              </div>
              <div style={{ height: 6, background: 'var(--bg-primary)', borderRadius: 3, overflow: 'hidden' }}>
                <div style={{
                  height: '100%',
                  width: `${incident.confidence * 100}%`,
                  background: incident.confidence >= 0.8 ? 'var(--success)' : 'var(--warning)',
                  borderRadius: 3,
                  transition: 'width 0.5s',
                }} />
              </div>
            </div>
          )}
          <div className="text-muted text-sm">Engine: {incident.diagnosed_by || '—'}</div>
          {incident.reasoning && (
            <p className="text-sm mt-16" style={{ color: 'var(--text-secondary)' }}>
              {incident.reasoning}
            </p>
          )}
        </div>

        {/* Remediation Audit */}
        <div className="card">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Remediation Audits
          </h3>
          {remediation_audits && remediation_audits.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              {remediation_audits.map((a, idx) => (
                <div key={a.id || idx} style={{ paddingBottom: idx < remediation_audits.length - 1 ? 16 : 0, borderBottom: idx < remediation_audits.length - 1 ? '1px solid var(--border)' : 'none' }}>
                  <div style={{ marginBottom: 8 }}>
                    <span className="text-muted text-sm">Action: </span>
                    <span className="mono">{a.action}</span>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <span className="text-muted text-sm">Verdict: </span>
                    <span className={`badge ${a.policy_verdict?.toLowerCase()}`}>
                      {a.policy_verdict}
                    </span>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <span className="text-muted text-sm">Execution: </span>
                    <span className="mono">{a.execution_status || 'Pending'}</span>
                  </div>
                  {a.failure_reason && (
                    <div style={{ marginBottom: 8 }}>
                      <span className="text-muted text-sm">Failure: </span>
                      <span style={{ color: 'var(--error)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                        {a.failure_reason}
                      </span>
                    </div>
                  )}
                  {a.failure_root_cause && (
                    <div style={{ marginBottom: 8 }}>
                      <span className="text-muted text-sm">Root Cause: </span>
                      <span className={`badge ${a.failure_root_cause === 'LOGIC' ? 'failed' : 'escalated'}`}>
                        {a.failure_root_cause}
                      </span>
                    </div>
                  )}
                  {a.pr_url && (
                    <div style={{ marginBottom: 8 }}>
                      <span className="text-muted text-sm">PR: </span>
                      <a href={a.pr_url} target="_blank" rel="noopener" style={{ color: 'var(--accent)' }}>
                        {a.pr_url.split('/').slice(-2).join('/')}
                      </a>
                    </div>
                  )}
                  {a.is_shadow === 'true' && (
                    <div className="status-pill shadow" style={{ marginTop: 8, display: 'inline-block' }}>🛡 Shadow Run</div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-muted">No remediation audit yet</p>
          )}
        </div>
      </div>

      {/* Resolved Target */}
      {resolved && (
        <div className="card mt-16">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            🎯 Resolved Target
          </h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
            <div>
              <div className="text-muted text-sm">Namespace</div>
              <div className="mono">{resolved.namespace}</div>
            </div>
            <div>
              <div className="text-muted text-sm">Deployment</div>
              <div className="mono">{resolved.deployment}</div>
            </div>
            <div>
              <div className="text-muted text-sm">Container</div>
              <div className="mono">{resolved.container}</div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 16, marginTop: 12 }}>
            <div>
              <span className="text-muted text-sm">Confidence: </span>
              <span className="mono" style={{ color: resolved.confidence >= 0.8 ? 'var(--success)' : 'var(--warning)' }}>
                {(resolved.confidence * 100).toFixed(0)}%
              </span>
            </div>
            <div>
              <span className="text-muted text-sm">Pod Pattern: </span>
              <span className="mono">{resolved.pod_pattern}</span>
            </div>
          </div>
          {resolved.validation && (
            <div style={{ display: 'flex', gap: 12, marginTop: 12, fontSize: 12 }}>
              <span style={{ color: resolved.validation.namespace_match ? 'var(--success)' : 'var(--error)' }}>
                {resolved.validation.namespace_match ? '✅' : '❌'} Namespace match
              </span>
              <span style={{ color: resolved.validation.pod_label_present ? 'var(--success)' : 'var(--warning)' }}>
                {resolved.validation.pod_label_present ? '✅' : '⚠️'} Pod label
              </span>
              <span style={{ color: resolved.validation.deployment_resolved ? 'var(--success)' : 'var(--error)' }}>
                {resolved.validation.deployment_resolved ? '✅' : '❌'} Deployment resolved
              </span>
            </div>
          )}
        </div>
      )}

      {/* Policy Breakdown */}
      {(policyDecision || remediation_audits?.[0]) && (
        <div className="card mt-16">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Policy Decision Breakdown
          </h3>
          <div className="policy-grid">
            <div className="policy-gate">
              <span className="gate-icon">{incident.confidence >= 0.8 ? '✅' : '❌'}</span>
              Confidence: {incident.confidence != null ? formatConfidence(incident.confidence) : '—'} (≥80%)
            </div>
            <div className="policy-gate">
              <span className="gate-icon">✅</span>
              Decision: {policyDecision?.decision || audit?.policy_verdict || 'Pending'}
            </div>
            <div className="policy-gate">
              <span className="gate-icon">✅</span>
              Namespace: {resolved?.namespace || 'autofixops'}
            </div>
            <div className="policy-gate">
              <span className="gate-icon">{audit.action ? '✅' : '❌'}</span>
              Action: {policyDecision?.action_type || audit?.action || 'N/A'}
            </div>
            <div className="policy-gate">
              <span className="gate-icon">{audit.is_shadow === 'true' ? '🛡' : '🟢'}</span>
              Mode: {audit?.is_shadow === 'true' ? 'Shadow' : 'Policy only'}
            </div>
          </div>
          {policyDecision?.reason && (
            <p className="text-sm text-muted mt-16">{policyDecision.reason}</p>
          )}
        </div>
      )}

      {/* Telemetry Context */}
      {telemetry_context && (
        <div className="card mt-16">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            📊 Telemetry Evidence
          </h3>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div style={{ background: 'var(--bg-secondary)', padding: 12, borderRadius: 8 }}>
              <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8 }}>CPU Usage (last 1m)</div>
              {telemetry_context.metrics?.cpu?.length > 0 ? (
                telemetry_context.metrics.cpu.map((series, i) => (
                  <div key={i} className="mono text-sm">
                    {series.value?.[1] ? `${parseFloat(series.value[1]).toFixed(4)} cores` : 'No data'}
                  </div>
                ))
              ) : (
                <div className="text-muted text-sm">No CPU data</div>
              )}
            </div>
            <div style={{ background: 'var(--bg-secondary)', padding: 12, borderRadius: 8 }}>
              <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8 }}>Memory Usage (last 5m)</div>
              {telemetry_context.metrics?.memory?.length > 0 ? (
                telemetry_context.metrics.memory.map((series, i) => (
                  <div key={i} className="mono text-sm">
                    {series.value?.[1] ? `${(parseFloat(series.value[1]) / 1024 / 1024).toFixed(1)} MB` : 'No data'}
                  </div>
                ))
              ) : (
                <div className="text-muted text-sm">No memory data</div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Escalation Approval */}
      {incident.status === 'ESCALATED' && (
        <div className="card mt-16" style={{ borderColor: 'var(--warning)' }}>
          <h3 style={{ color: 'var(--warning)', marginBottom: 8 }}>⚠️ Human Approval Required</h3>
          <p className="text-sm text-muted mb-16">
            This incident was escalated. Review the diagnosis and approve to trigger remediation.
          </p>
          <button className="btn btn-success" onClick={handleApprove} disabled={approving}>
            {approving ? <span className="loading-spinner" /> : '✓ Approve & Execute'}
          </button>
        </div>
      )}

      {/* Rollback */}
      {audit && audit.execution_status && incident.status !== 'ESCALATED' && (
        <div className="card mt-16" style={{ borderColor: 'rgba(220,38,38,0.3)' }}>
          <h3 style={{ color: 'var(--error)', marginBottom: 8, fontSize: 14 }}>🔄 Rollback</h3>
          <p className="text-sm text-muted mb-16">
            Revert the last remediation by restoring previous resource values.
          </p>
          <button
            className="btn btn-danger"
            onClick={handleRollback}
            disabled={rollingBack}
            style={{ fontSize: 12, padding: '8px 16px' }}
          >
            {rollingBack ? <span className="loading-spinner" /> : '⚡ Trigger Rollback'}
          </button>
        </div>
      )}

      {toast && <div className={`toast ${toast.type}`}>{toast.msg}</div>}
    </div>
  );
}
