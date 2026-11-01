// Direct links to the monitored target application. They stay usable whatever the status says, so
// an outage can be inspected in the browser. The status comes from Kubernetes (via the card's
// existing poll), never from the links themselves.
import { TARGET_APP, targetAppStatus } from '../lib/targetApp.mjs';

const LABEL = { fontSize: 12, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' };
const LINKS = [
  { key: 'app', label: 'Open Target App' },
  { key: 'health', label: 'Health Check' },
  { key: 'ready', label: 'Readiness Check' },
];

function ExternalIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 3h7v7" /><path d="M10 14 21 3" /><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" />
    </svg>
  );
}

export default function TargetAppLinks({ evidence, loading = false }) {
  const status = loading ? { label: 'Checking', color: 'var(--text-muted)', detail: 'waiting for Kubernetes status' } : targetAppStatus(evidence);
  return (
    <div style={{ marginTop: 10, padding: '10px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius)', display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
      <div>
        <div style={LABEL}>Target application</div>
        <div className="text-sm" style={{ marginTop: 2 }}>
          <span className="mono" style={{ color: 'var(--text-primary)' }}>{TARGET_APP.base}</span>
          <span className="text-muted"> · port <span className="mono">{TARGET_APP.port}</span> · </span>
          <span title={`${status.detail} (reported by Kubernetes)`} style={{ color: status.color, fontWeight: 600 }}>
            <span aria-hidden="true" style={{ display: 'inline-block', width: 7, height: 7, borderRadius: '50%', background: status.color, marginRight: 5, verticalAlign: 'middle' }} />
            {status.label}
          </span>
        </div>
        <div className="text-muted" style={{ fontSize: 12, marginTop: 2 }}>
          On Docker Desktop the links need the browser-access launcher (<span className="mono">START_TARGET_APP.bat</span>) running.
        </div>
      </div>
      <div className="flex" style={{ gap: 8, flexWrap: 'wrap' }}>
        {LINKS.map(({ key, label }) => (
          <a key={key} href={TARGET_APP[key]} target="_blank" rel="noopener noreferrer" title={TARGET_APP[key]}
            aria-label={`${label} (opens ${TARGET_APP[key]} in a new tab)`}
            className="btn btn-outline" style={{ padding: '6px 12px', textDecoration: 'none' }}>
            <ExternalIcon />{label}
          </a>
        ))}
      </div>
    </div>
  );
}
