'use client';
import { useState } from 'react';
import { STAGES, pipelineStates } from '../lib/pipeline.mjs';

const STATE_TEXT = {
  completed: 'completed (confirmed by the backend)', active: 'in progress', warning: 'needs a human',
  failed: 'failed here', skipped: 'not used for this incident', idle: 'not reached',
};

// `run` (optional): the Stage 3-4 remediation run, so deployment/verification progress is shown
// only once the backend has recorded it.
export default function PipelineFlow({ incident, run = null }) {
  const states = pipelineStates(incident, run);
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
          ? <><strong className="flow-highlight">{STAGES[shown].label}</strong> ({STATE_TEXT[states[shown]]}) — {STAGES[shown].desc}</>
          : 'Hover or select a stage to see what it does.'}
      </div>
    </div>
  );
}
