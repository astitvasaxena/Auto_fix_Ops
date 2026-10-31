'use client';
import { useState, useEffect, useRef } from 'react';
import { api } from '../../lib/api';
import KubernetesTargetCard from '../../components/KubernetesTargetCard';

const FAULTS = [
  {
    id: 'memory_leak',
    name: 'Memory Leak',
    icon: '🧠',
    desc: 'Triggers /leak endpoint. Allocates memory until OOMKilled.',
    severity: 'HIGH',
  },
  {
    id: 'cpu_spike',
    name: 'CPU Spike',
    icon: '🔥',
    desc: 'Triggers /cpu endpoint. Burns CPU for 60s via tight loops.',
    severity: 'MEDIUM',
  },
  {
    id: 'crash_loop',
    name: 'Crash Loop',
    icon: '💀',
    desc: 'Triggers /crash endpoint. Forces process exit → CrashLoopBackOff.',
    severity: 'HIGH',
  },
];

const COOLDOWN_SECONDS = 60;

function CooldownTimer({ faultId, cooldowns }) {
  const remaining = cooldowns[faultId] || 0;
  if (remaining <= 0) return null;

  const pct = (remaining / COOLDOWN_SECONDS) * 100;

  return (
    <div style={{
      marginTop: 8,
      background: 'rgba(245,158,11,0.08)',
      border: '1px solid rgba(245,158,11,0.2)',
      borderRadius: 'var(--radius)',
      padding: '8px 12px',
      fontSize: 12,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ color: 'var(--warning)' }}>⏳ Cooldown</span>
        <span className="mono" style={{ color: 'var(--warning)' }}>{remaining}s</span>
      </div>
      <div style={{ height: 4, background: 'rgba(245,158,11,0.1)', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: 'var(--warning)',
          borderRadius: 2,
          transition: 'width 1s linear',
        }} />
      </div>
    </div>
  );
}

export default function ChaosPage() {
  const [targetUrl, setTargetUrl] = useState('http://localhost:8001');
  const [confirms, setConfirms] = useState({});
  const [results, setResults] = useState({});
  const [loading, setLoading] = useState({});
  const [cooldowns, setCooldowns] = useState({});
  const [systemMode, setSystemMode] = useState('ACTIVE');
  const intervalRef = useRef(null);

  // Fetch system mode
  useEffect(() => {
    api.getSystemMode().then((d) => setSystemMode(d.system_mode)).catch(() => {});
  }, []);

  // Cooldown ticker
  useEffect(() => {
    intervalRef.current = setInterval(() => {
      setCooldowns((prev) => {
        const next = {};
        let hasActive = false;
        for (const [key, val] of Object.entries(prev)) {
          if (val > 0) {
            next[key] = val - 1;
            hasActive = true;
          }
        }
        return hasActive ? next : {};
      });
    }, 1000);
    return () => clearInterval(intervalRef.current);
  }, []);

  const handleInject = async (faultId) => {
    if (confirms[faultId] !== 'CONFIRM') {
      setResults((r) => ({ ...r, [faultId]: { type: 'error', msg: 'Type CONFIRM to proceed' } }));
      return;
    }

    if ((cooldowns[faultId] || 0) > 0) {
      setResults((r) => ({ ...r, [faultId]: { type: 'error', msg: `Rate limited. Wait ${cooldowns[faultId]}s.` } }));
      return;
    }

    setLoading((l) => ({ ...l, [faultId]: true }));
    try {
      const res = await api.injectChaos({
        fault_type: faultId,
        target_url: targetUrl,
        confirmation: 'CONFIRM',
      });
      setResults((r) => ({ ...r, [faultId]: {
        type: 'success',
        msg: `Injected: ${res.target} → ${res.response_code}`
      }}));
      setConfirms((c) => ({ ...c, [faultId]: '' }));
      // Start cooldown timer
      setCooldowns((c) => ({ ...c, [faultId]: res.cooldown_seconds || COOLDOWN_SECONDS }));
    } catch (e) {
      // Parse retry-after from 429 error
      const match = e.message.match(/Wait (\d+)s/);
      if (match) {
        setCooldowns((c) => ({ ...c, [faultId]: parseInt(match[1]) }));
      }
      setResults((r) => ({ ...r, [faultId]: { type: 'error', msg: e.message } }));
    }
    setLoading((l) => ({ ...l, [faultId]: false }));
  };

  const isDisabled = systemMode === 'DISABLED';

  return (
    <div className="page">
      <div className="page-header">
        <h2 style={{ color: 'var(--error)' }}>⚠️ Chaos Injection</h2>
        <p>Controlled fault injection for testing the remediation pipeline</p>
      </div>

      {/* System Disabled Banner */}
      {isDisabled && (
        <div style={{
          background: 'rgba(220,38,38,0.12)',
          border: '1px solid rgba(220,38,38,0.4)',
          borderRadius: 'var(--radius)',
          padding: '16px 20px',
          marginBottom: 24,
          fontSize: 14,
          color: 'var(--error)',
          textAlign: 'center',
          fontWeight: 600,
        }}>
          🛑 System is DISABLED — Chaos injection is blocked. Enable the system from Settings to proceed.
        </div>
      )}

      {/* Warning Banner */}
      <div style={{
        background: 'rgba(220,38,38,0.08)',
        border: '1px solid rgba(220,38,38,0.3)',
        borderRadius: 'var(--radius)',
        padding: '14px 18px',
        marginBottom: 24,
        fontSize: 13,
        color: 'var(--error)',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}>
        <span style={{ fontSize: 18 }}>🚨</span>
        <div>
          <strong>DANGER ZONE</strong> — These actions inject real faults into the target application.
          Only allowed in configured namespaces. Rate limited: 1 injection per 60 seconds per fault type.
        </div>
      </div>

      {/* Kubernetes target: live pod health and the reproducible crash-loop fault */}
      <KubernetesTargetCard showChaos />

      {/* Target URL */}
      <div className="card mb-16">
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label>Target Application URL</label>
          <input
            type="text"
            value={targetUrl}
            onChange={(e) => setTargetUrl(e.target.value)}
            placeholder="http://target-app.autofixops.svc:8000"
            style={{ borderColor: 'var(--danger-red)' }}
            disabled={isDisabled}
          />
        </div>
      </div>

      {/* Chaos Cards */}
      <div className="chaos-zone">
        <div className="chaos-header">
          <span>💥</span> Fault Injectors
        </div>
        <div className="chaos-cards">
          {FAULTS.map((fault) => {
            const onCooldown = (cooldowns[fault.id] || 0) > 0;
            return (
              <div className="chaos-card" key={fault.id} style={{ opacity: isDisabled ? 0.5 : 1 }}>
                <h4>{fault.icon} {fault.name}</h4>
                <p>{fault.desc}</p>
                <div className="severity-row">
                  Severity: <span style={{ color: fault.severity === 'HIGH' ? 'var(--error)' : 'var(--warning)', fontWeight: 600 }}>
                    {fault.severity}
                  </span>
                </div>
                <div className="confirm-input">
                  <input
                    type="text"
                    placeholder='Type "CONFIRM" to unlock'
                    value={confirms[fault.id] || ''}
                    onChange={(e) => setConfirms((c) => ({ ...c, [fault.id]: e.target.value }))}
                    disabled={isDisabled || onCooldown}
                  />
                  <button
                    className="btn btn-danger"
                    disabled={isDisabled || loading[fault.id] || confirms[fault.id] !== 'CONFIRM' || onCooldown}
                    onClick={() => handleInject(fault.id)}
                  >
                    {loading[fault.id] ? <span className="loading-spinner" />
                      : onCooldown ? `⏳ ${cooldowns[fault.id]}s`
                      : '⚡ Inject Fault'}
                  </button>
                </div>
                <CooldownTimer faultId={fault.id} cooldowns={cooldowns} />
                {results[fault.id] && (
                  <div style={{
                    marginTop: 12,
                    padding: '10px 14px',
                    borderRadius: 'var(--radius)',
                    fontSize: 12,
                    fontFamily: 'var(--font-mono)',
                    background: results[fault.id].type === 'success' ? 'var(--success-bg)' : 'var(--error-bg)',
                    color: results[fault.id].type === 'success' ? 'var(--success)' : 'var(--error)',
                    border: `1px solid ${results[fault.id].type === 'success' ? 'rgba(16,185,129,0.2)' : 'rgba(239,68,68,0.2)'}`,
                  }}>
                    {results[fault.id].msg}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
