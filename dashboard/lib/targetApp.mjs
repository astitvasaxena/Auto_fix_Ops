// Where a browser reaches the monitored target application, and what the dashboard may say about
// its state. On Docker Desktop the NodePort is not published on Windows: the browser-access
// launcher (START_TARGET_APP.bat) forwards localhost:30081 to service target-app-nodeport.
// NEXT_PUBLIC_TARGET_APP_URL is inlined when the dashboard is built (like NEXT_PUBLIC_API_URL).

export const DEFAULT_TARGET_APP_URL = 'http://localhost:30081';

// Every link is derived from one base URL. The app has no page at "/", so "Open Target App" goes to
// its interactive API page (/docs). Anything that is not a plain http(s) URL falls back to the default.
export function targetAppLinks(configured) {
  let url;
  try {
    url = new URL((configured || '').trim() || DEFAULT_TARGET_APP_URL);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') throw new Error('not http(s)');
  } catch {
    url = new URL(DEFAULT_TARGET_APP_URL);
  }
  const base = `${url.origin}${url.pathname}`.replace(/\/+$/, '');
  return {
    base,
    host: url.host,
    port: url.port || (url.protocol === 'https:' ? '443' : '80'),
    app: `${base}/docs`,
    health: `${base}/health`,
    ready: `${base}/ready`,
  };
}

export const TARGET_APP = targetAppLinks(process.env.NEXT_PUBLIC_TARGET_APP_URL);

// A short status from what Kubernetes reports (GET /api/v1/target/kubernetes). It describes the pods,
// not whether this browser can reach them: the links work only while the launcher's tunnel is up.
export function targetAppStatus(evidence) {
  if (!evidence?.available) return { label: 'Unknown', color: 'var(--text-muted)', detail: 'Kubernetes status unavailable' };
  const s = evidence.summary || {};
  if (s.crash_loop || !s.pods) {
    return { label: 'Unreachable', color: 'var(--error)', detail: s.crash_loop ? 'container crash-looping' : 'no pod running' };
  }
  if (s.ready_pods === s.pods) return { label: 'Healthy', color: 'var(--success)', detail: `${s.ready_pods}/${s.pods} pods ready` };
  return { label: 'Not Ready', color: 'var(--warning)', detail: `${s.ready_pods}/${s.pods} pods ready` };
}
