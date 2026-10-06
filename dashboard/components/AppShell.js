'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const NAV = [
  { href: '/incidents', icon: '⚡', label: 'Incidents' },
  { href: '/metrics', icon: '📊', label: 'Metrics' },
  { href: '/chaos', icon: '💥', label: 'Chaos' },
  { href: '/onboard', icon: '⚙️', label: 'Settings' },
];

function KillSwitchBanner({ systemMode, onModeChange }) {
  const [reason, setReason] = useState('');

  if (systemMode === 'ACTIVE') return null;

  const isDisabled = systemMode === 'DISABLED';

  return (
    <div style={{
      background: isDisabled ? 'rgba(220,38,38,0.15)' : 'rgba(245,158,11,0.12)',
      border: `1px solid ${isDisabled ? 'rgba(220,38,38,0.5)' : 'rgba(245,158,11,0.4)'}`,
      borderRadius: 'var(--radius)',
      padding: '12px 18px',
      display: 'flex',
      alignItems: 'center',
      gap: 12,
      fontSize: 13,
      color: isDisabled ? 'var(--error)' : 'var(--warning)',
    }}>
      <span style={{ fontSize: 20 }}>{isDisabled ? '🛑' : '🛡'}</span>
      <div style={{ flex: 1 }}>
        <strong>{isDisabled ? 'SYSTEM DISABLED' : 'SHADOW MODE'}</strong>
        {isDisabled
          ? ' — All execution halted. No tasks, PRs, or chaos injections.'
          : ' — PRs are drafts. No auto-merge.'}
      </div>
      {isDisabled && (
        <button
          className="btn btn-outline"
          style={{ fontSize: 11, padding: '4px 12px', borderColor: 'var(--success)', color: 'var(--success)' }}
          onClick={() => onModeChange('ACTIVE')}
        >
          Re-enable
        </button>
      )}
    </div>
  );
}

function TargetHealthPill() {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    const load = () => api.getTargetHealth()
      .then((h) => setHealth(h.status))
      .catch(() => setHealth('UNKNOWN'));
    load();
    const interval = setInterval(load, 15000);
    const cleanup = api.listenEvents((event) => {
      if (event.type === 'incident.created') load();
    });
    return () => { clearInterval(interval); cleanup(); };
  }, []);

  if (!health) return null;
  const cls = health === 'HEALTHY' ? 'live' : health === 'DOWN' ? 'open' : 'disconnected';
  return <span className={`status-pill ${cls}`}>Target: {health}</span>;
}

function TopBar({ onModeChange }) {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    const load = () => api.getStatus().then(setStatus).catch(() => {});
    load();
    const cleanup = api.listenEvents((event) => {
      if (event.type === 'system.mode_changed' || event.type === 'circuit_breaker.state_changed') {
        load();
      }
    });

    const localUpdate = () => load();
    window.addEventListener('local_mode_changed', localUpdate);

    return () => {
      cleanup();
      window.removeEventListener('local_mode_changed', localUpdate);
    };
  }, []);

  if (!status) {
    return (
      <>
        <div className="top-bar">
          <span className="loading-spinner" />
        </div>
      </>
    );
  }

  const mode = status.system_mode || 'ACTIVE';
  const cbState = status.circuit_breaker || 'CLOSED';
  const cbClass = cbState.toLowerCase().replace('_', '-');
  const ghClass = status.github_connected ? 'connected' : 'disconnected';

  const modeColors = { ACTIVE: 'green', SHADOW: 'yellow', DISABLED: 'red' };
  const modeLabels = { ACTIVE: '🟢 ACTIVE', SHADOW: '🛡 SHADOW', DISABLED: '🛑 DISABLED' };

  return (
    <>
      <KillSwitchBanner systemMode={mode} onModeChange={onModeChange} />
      <div className="top-bar">
        <div className="status-pill" style={{ marginRight: 'auto' }}>
          <span className={`dot ${modeColors[mode] || 'yellow'}`} />
          {status.target_namespace}
        </div>

        <select
          value={mode}
          onChange={(e) => onModeChange(e.target.value)}
          style={{
            background: 'var(--bg-secondary)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            color: mode === 'DISABLED' ? 'var(--error)' : mode === 'SHADOW' ? 'var(--warning)' : 'var(--success)',
            fontSize: 11,
            padding: '4px 8px',
            fontWeight: 600,
            cursor: 'pointer',
          }}
        >
          <option value="ACTIVE">🟢 ACTIVE</option>
          <option value="SHADOW">🛡 SHADOW</option>
          <option value="DISABLED">🛑 DISABLED</option>
        </select>

        <TargetHealthPill />

        <span className={`status-pill ${cbClass}`}>
          CB: {cbState}
        </span>

        <span className={`status-pill ${ghClass}`}>
          {status.github_connected ? '✓ GitHub' : '✗ GitHub'}
        </span>
      </div>
    </>
  );
}

function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <h1>AutoFixOps</h1>
        <span>Control Plane</span>
      </div>
      <nav className="sidebar-nav">
        {NAV.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={pathname?.startsWith(item.href) ? 'active' : ''}
          >
            <span className="nav-icon">{item.icon}</span>
            {item.label}
          </Link>
        ))}
      </nav>
    </aside>
  );
}

export default function AppShell({ children }) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const handleModeChange = async (newMode) => {
    try {
      await api.setSystemMode(newMode, newMode === 'DISABLED' ? 'Manual kill switch from dashboard' : '');
      // Force re-render by reloading status
      window.location.reload();
    } catch (e) {
      alert(`Failed to change mode: ${e.message}`);
    }
  };

  if (!mounted) {
    return (
      <div className="app-layout">
        <aside className="sidebar">
          <div className="sidebar-logo">
            <h1>AutoFixOps</h1>
            <span>Control Plane</span>
          </div>
          <nav className="sidebar-nav">
            {NAV.map((item) => (
              <a key={item.href} href={item.href}>
                <span className="nav-icon">{item.icon}</span>
                {item.label}
              </a>
            ))}
          </nav>
        </aside>
        <main className="main-content">
          <div className="top-bar">
            <span className="loading-spinner" />
          </div>
          {children}
        </main>
      </div>
    );
  }

  return (
    <div className="app-layout">
      <Sidebar />
      <main className="main-content">
        <TopBar onModeChange={handleModeChange} />
        {children}
      </main>
    </div>
  );
}
