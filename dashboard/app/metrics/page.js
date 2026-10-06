'use client';
import { useEffect, useState } from 'react';
import { api } from '../../lib/api';
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, BarChart, Bar, XAxis, YAxis, CartesianGrid } from 'recharts';

const COLORS = { success: '#10b981', error: '#ef4444', warning: '#f59e0b', info: '#3b82f6', accent: '#FC6C26', muted: '#555566' };

export default function MetricsPage() {
  const [metrics, setMetrics] = useState(null);
  const [window, setWindow] = useState('all_time');
  const [grafanaUrl, setGrafanaUrl] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = () => {
      api.getMetrics().then(setMetrics).catch(() => {}).finally(() => setLoading(false));
    };
    load();
    const cleanup = api.listenEvents((event) => {
      if (event.type.startsWith('incident.') || event.type.startsWith('remediation.')) {
        load();
      }
    });
    return cleanup;
  }, []);

  if (loading) return <div className="page"><div className="empty-state"><span className="loading-spinner" /></div></div>;

  const m = metrics?.[window] || {};

  const engineData = [
    { name: 'Rule Engine', value: Math.round((m.rule_engine_hit_rate || 0) * 100) },
    { name: 'AI Fallback', value: Math.round((m.ai_fallback_rate || 0) * 100) },
    { name: 'Undiagnosed', value: Math.max(0, 100 - Math.round(((m.rule_engine_hit_rate || 0) + (m.ai_fallback_rate || 0)) * 100)) },
  ];

  const policyData = [
    { name: 'Approved', value: Math.round((m.policy_approval_rate || 0) * 100), fill: COLORS.success },
    { name: 'Escalated', value: Math.round((m.escalation_rate || 0) * 100), fill: COLORS.warning },
    { name: 'Rejected', value: Math.max(0, 100 - Math.round(((m.policy_approval_rate || 0) + (m.escalation_rate || 0)) * 100)), fill: COLORS.error },
  ];

  return (
    <div className="page">
      <div className="page-header flex justify-between items-center">
        <div>
          <h2>Pipeline Metrics</h2>
          <p>System health and decision analytics</p>
        </div>
        <div className="filter-bar" style={{ marginBottom: 0 }}>
          {['all_time', 'last_24h', 'last_7d'].map((w) => (
            <button key={w} className={`filter-chip ${window === w ? 'active' : ''}`} onClick={() => setWindow(w)}>
              {w === 'all_time' ? 'All Time' : w === 'last_24h' ? '24h' : '7d'}
            </button>
          ))}
        </div>
      </div>

      {/* Top Cards */}
      <div className="card-grid">
        <div className="metric-card">
          <div className="label">Total Incidents</div>
          <div className="value info">{m.total_incidents || 0}</div>
        </div>
        <div className="metric-card">
          <div className="label">Resolved</div>
          <div className="value success">{m.resolved || 0}</div>
        </div>
        <div className="metric-card">
          <div className="label">Failed</div>
          <div className="value error">{m.failed || 0}</div>
        </div>
        <div className="metric-card">
          <div className="label">Escalated</div>
          <div className="value warning">{m.escalated || 0}</div>
        </div>
        <div className="metric-card">
          <div className="label">Verification Success</div>
          <div className="value info">{m.verification_success_rate != null ? `${Math.round(m.verification_success_rate * 100)}%` : '0%'}</div>
        </div>
        <div className="metric-card">
          <div className="label">Shadow Runs</div>
          <div className="value" style={{ color: 'var(--accent)' }}>{m.shadow_runs || 0}</div>
        </div>
      </div>

      {/* Charts */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 24 }}>
        <div className="card">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 16, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Diagnosis Engine Split
          </h3>
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie data={engineData} cx="50%" cy="50%" innerRadius={55} outerRadius={80} dataKey="value" paddingAngle={3}>
                <Cell fill={COLORS.success} />
                <Cell fill={COLORS.accent} />
                <Cell fill={COLORS.muted} />
              </Pie>
              <Tooltip contentStyle={{ background: '#12121a', border: '1px solid #1e1e2e', borderRadius: 8, fontSize: 12 }} />
            </PieChart>
          </ResponsiveContainer>
          <div style={{ display: 'flex', justifyContent: 'center', gap: 16, marginTop: 8 }}>
            {engineData.map((d, i) => (
              <span key={d.name} style={{ fontSize: 11, color: [COLORS.success, COLORS.accent, COLORS.muted][i] }}>
                ● {d.name}: {d.value}%
              </span>
            ))}
          </div>
        </div>

        <div className="card">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 16, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Policy Decisions
          </h3>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={policyData} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" stroke="#1e1e2e" />
              <XAxis type="number" domain={[0, 100]} tick={{ fill: '#555566', fontSize: 11 }} />
              <YAxis type="category" dataKey="name" tick={{ fill: '#8888a0', fontSize: 12 }} width={80} />
              <Tooltip contentStyle={{ background: '#12121a', border: '1px solid #1e1e2e', borderRadius: 8, fontSize: 12 }} />
              <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                {policyData.map((d, i) => <Cell key={i} fill={d.fill} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Grafana Embed */}
      <div className="card">
        <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
          Grafana Dashboard
        </h3>
        <div className="form-group">
          <label>Grafana iframe URL (optional)</label>
          <input
            type="text"
            value={grafanaUrl}
            onChange={(e) => setGrafanaUrl(e.target.value)}
            placeholder="https://grafana.yourdomain.com/d/abc/dashboard?orgId=1&kiosk"
          />
        </div>
        {grafanaUrl ? (
          <iframe
            src={grafanaUrl}
            width="100%"
            height="400"
            frameBorder="0"
            style={{ borderRadius: 'var(--radius)', marginTop: 8 }}
          />
        ) : (
          <div className="empty-state" style={{ padding: 40 }}>
            <div className="icon">📈</div>
            <p>Paste a Grafana kiosk URL above to embed your dashboard</p>
          </div>
        )}
      </div>
    </div>
  );
}
