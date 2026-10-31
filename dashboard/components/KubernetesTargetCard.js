'use client';
// Live health of the Kubernetes target (read-only kubectl, via GET /api/v1/target/kubernetes)
// and the crash-loop chaos control. Injection only changes the target; the incident must be
// detected by Prometheus/Alertmanager like any real failure.
import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';
import { podHealth } from '../lib/pipeline.mjs';

const POLL_MS = 5000;
const LABEL = { fontSize: 12, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' };

export default function KubernetesTargetCard({ showChaos = true }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const load = useCallback(() => {
    api.getKubernetesTarget().then((d) => { setData(d); setError(null); }).catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const inject = async (fault) => {
    setBusy(true);
    try {
      setResult(await api.injectKubernetesChaos(fault, confirm));
      setConfirm('');
    } catch (e) {
      setResult({ error: e.message });
    }
    setBusy(false);
    load();
  };

  if (!data) return error ? <div className="card mb-16 text-sm" style={{ color: 'var(--error)' }}>{error}</div> : null;
  const ev = data.evidence;
  const health = podHealth(ev);
  const s = ev?.summary || {};

  return (
    <div className="card mb-16">
      <div className="flex justify-between items-center">
        <div style={LABEL}>Kubernetes target</div>
        <span className="mono" style={{ color: health.color, border: `1px solid ${health.color}`, borderRadius: 6, padding: '3px 10px', fontSize: 12, fontWeight: 700 }}>
          {health.label}
        </span>
      </div>
      {!ev?.available ? (
        <p className="text-sm text-muted" style={{ marginTop: 8 }}>{ev?.reason}</p>
      ) : (
        <div className="text-sm" style={{ marginTop: 8, display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
          <div><div className="text-muted">Deployment</div><span className="mono">{s.namespace}/{s.deployment}</span></div>
          <div><div className="text-muted">Pods ready</div><span className="mono">{s.ready_pods}/{s.pods}</span></div>
          <div><div className="text-muted">Restarts</div><span className="mono">{s.restart_count}</span>
            {s.waiting_reason && <span style={{ color: 'var(--error)' }}> · {s.waiting_reason}</span>}</div>
          <div><div className="text-muted">Memory limit</div><span className="mono">{s.live_memory_limit}</span>
            {s.last_termination_reason && <span className="text-muted"> · last exit {s.last_termination_reason} ({s.last_exit_code})</span>}</div>
        </div>
      )}
      {data.chaos && !data.chaos.error && (
        <p className="text-sm text-muted" style={{ marginTop: 8 }}>
          Crash-loop chaos: start-up memory <span className="mono">{data.chaos.startup_memory_mb} MB</span>
          {data.chaos.startup_memory_mb > 0 ? ' (active)' : ' (off)'}
        </p>
      )}
      {showChaos && ev?.available && (
        <div style={{ marginTop: 12 }}>
          {!data.chaos_enabled ? (
            <p className="text-sm text-muted">Kubernetes chaos is off (set <span className="mono">CHAOS_K8S_ENABLED=true</span> for the API), or use <span className="mono">python scripts/k8s_demo.py chaos crashloop</span>.</p>
          ) : (
            <div className="flex" style={{ gap: 8 }}>
              <div className="confirm-input" style={{ flex: 1 }}>
                <input value={confirm} placeholder="Type CONFIRM" onChange={(e) => setConfirm(e.target.value)} />
              </div>
              <button className="btn btn-danger" disabled={busy || confirm !== 'CONFIRM'} onClick={() => inject('crash_loop')}>💥 Inject crash loop</button>
              <button className="btn btn-outline" disabled={busy || confirm !== 'CONFIRM'} onClick={() => inject('reset')}>Reset chaos</button>
            </div>
          )}
          {result && (
            <p className="text-sm" style={{ marginTop: 8, color: result.error ? 'var(--error)' : 'var(--text-secondary)' }}>
              {result.error || `${result.expected || 'Chaos reset.'} ${result.note || ''}`}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
