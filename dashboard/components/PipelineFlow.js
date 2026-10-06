'use client';
import { useState } from 'react';

const STAGES = [
  { key: 'target', label: 'Target App', icon: '🎯', desc: 'The monitored workload. It exposes /metrics and /health; chaos faults are injected here.' },
  { key: 'prometheus', label: 'Prometheus', icon: '📈', desc: 'Scrapes the target every 15s and evaluates alert rules (memory, CPU, restarts, up == 0).' },
  { key: 'alertmanager', label: 'AlertManager', icon: '🔔', desc: 'Groups firing alerts and posts them as a webhook to the AutoFixOps API.' },
  { key: 'api', label: 'AutoFixOps API', icon: '⚡', desc: 'Ingests the alert, de-duplicates it and creates an incident (INGESTED).' },
  { key: 'context', label: 'Context', icon: '🧩', desc: 'Gathers metrics and resolves the target namespace / deployment / container.' },
  { key: 'diagnosis', label: 'Diagnosis', icon: '🧠', desc: 'Rule engine first; unknown anomalies fall through to AI diagnosis with a confidence floor.' },
  { key: 'policy', label: 'Policy', icon: '🛡', desc: 'Safety gates: kill switch, allowlist, confidence, protected namespaces, anti-thrash, circuit breaker.' },
  { key: 'remediation', label: 'Remediation', icon: '🔧', desc: 'Applies bounded manifest patch and creates Pull Request for automated recovery.' },
  { key: 'verification', label: 'Verification', icon: '✅', desc: 'Verifies actual workload health and telemetry stability before resolving incident.' },
];

// Index of the stage the incident is currently in (from existing incident status).
const STATUS_STAGE = {
  INGESTED: 3,
  CONTEXT_BUILT: 4,
  DIAGNOSED: 5,
  POLICY_APPROVED: 6,
  REMEDIATING: 7,
  PENDING_PR_MERGE: 7,
  VERIFIED: 8,
  RESOLVED: 8,
};

function stageStates(status) {
  const states = STAGES.map(() => 'idle');
  if (!status) return states;

  if (status === 'ESCALATED') {
    for (let i = 0; i < 6; i++) states[i] = 'completed';
    states[6] = 'warning';
    return states;
  }

  if (status === 'FAILED') {
    for (let i = 0; i < 8; i++) states[i] = 'completed';
    states[8] = 'failed';
    return states;
  }

  const current = STATUS_STAGE[status];
  if (current != null) {
    for (let i = 0; i < current; i++) states[i] = 'completed';
    states[current] = status === 'RESOLVED' ? 'completed' : 'active';
    return states;
  }

  return states;
}

export default function PipelineFlow({ incident }) {
  const states = stageStates(incident?.status);
  const activeIdx = states.indexOf('active');
  const [selected, setSelected] = useState(null);
  const shown = selected ?? (activeIdx >= 0 ? activeIdx : null);

  return (
    <div className="card flow-card">
      <div className="flow-header">
        <span className="flow-title">Self-healing pipeline</span>
        <span className="text-muted text-sm">
          {incident
            ? <>Latest: <span className="mono flow-highlight">{incident.alert_name}</span> · {incident.status.replace(/_/g, ' ')}</>
            : 'Waiting for an incident'}
        </span>
      </div>

      <div className="flow-track">
        {STAGES.map((s, i) => (
          <div key={s.key} className="flow-step">
            {i > 0 && <span className={`flow-link ${states[i] !== 'idle' ? 'lit' : ''} ${states[i] === 'active' ? 'flowing' : ''}`} />}
            <button
              type="button"
              className={`flow-node ${states[i]} ${shown === i ? 'selected' : ''}`}
              onMouseEnter={() => setSelected(i)}
              onFocus={() => setSelected(i)}
              onClick={() => setSelected(i)}
              aria-pressed={shown === i}
            >
              <span className="flow-icon">{s.icon}</span>
              <span className="flow-label">{s.label}</span>
            </button>
          </div>
        ))}
      </div>

      <div className="flow-desc">
        {shown != null
          ? <><strong className="flow-highlight">{STAGES[shown].label}</strong> — {STAGES[shown].desc}</>
          : 'Hover or select a stage to see what it does.'}
      </div>
    </div>
  );
}
