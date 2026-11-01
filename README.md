# AutoFixOps
**An evidence-grounded, policy-safe self-healing framework for Kubernetes incident response**

AutoFixOps watches a workload with Prometheus, receives alerts from Alertmanager,
gathers telemetry, retrieves official runbook guidance (RAG), lets a **local LLM**
(Ollama `qwen2.5:3b`) diagnose the incident, proposes a **bounded, sandbox-validated**
fix, and — only after a **human approval** and a merged GitHub pull request — applies
that one change through a controlled deployment and verifies that the workload recovered.

```
Target App → Prometheus → Alertmanager → AutoFixOps API → Context → Diagnosis
           → Policy (human approval) → Remediation (PR + controlled deploy) → Verification
```

## Implementation status

| Stage | What | Status |
|---|---|---|
| 1 | GitHub Actions CI (offline tests, config validation, dashboard build) | Done |
| — | Alert ingestion, context, rule + AI diagnosis, runbook RAG | Done; telemetry collection fixed (real memory/CPU peaks) |
| 2 | AI patch proposals: confidence routing (0.80), human approval of generation, 16 mandatory safety checks | Done; a genuine `VALIDATED` proposal was produced from a real Alertmanager incident |
| 3 | Human approval of the exact proposal, audit trail, GitHub branch + commit + PR, duplicate prevention | Implemented and tested with a fake GitHub; a real PR needs your token and repository |
| 4 | Controlled `kubectl` deployment (allowlist, dry run, drift-safe JSON patch, lock), CI gate, recovery verification with stabilization window, rollback + revert PR | Implemented; tested with fakes and wired to a real Docker Desktop cluster (see the runbook for what has run live) |
| — | Crash-loop demo: reproducible chaos, read-only Kubernetes evidence, crash loops remediated only on OOMKilled or measured memory pressure | Implemented and run against the real cluster |
| — | Dashboard: backend-confirmed pipeline, approval controls, PR/deploy/verification status, incident timeline | Done |

See [docs/RUNNING_ON_WINDOWS.md](docs/RUNNING_ON_WINDOWS.md) for exact commands and the
end-to-end demonstration, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## Quick start (Windows, PowerShell)

```powershell
python -m pip install -r requirements.txt; cd dashboard; npm install; cd ..
Copy-Item .env.example .env          # edit it; never commit .env
docker compose up -d
python scripts/migrate_schema.py
python -m uvicorn kubernetes_integration.target_app.app:app --port 8001   # terminal 1
python -m uvicorn api.main:app --host 0.0.0.0 --port 8010                  # terminal 2
python -m celery -A workers.celery_app worker --loglevel=info --pool=solo  # terminal 3
cd dashboard; npm run dev                                                   # terminal 4 → http://localhost:3000
```

## Browser access to the Kubernetes target app

Docker Desktop's kind-based cluster does not publish NodePorts on Windows, so
`http://localhost:30081` refuses connections on its own. **`START_TARGET_APP.bat`** keeps a
`kubectl port-forward` from `127.0.0.1:30081` to service `autofixops/target-app-nodeport` port 80
(context `docker-desktop`) for the whole demo, through Memory Leak, CPU Spike and Crash Loop and
their remediation. You never restart it by hand.

* **Start:** double-click `START_TARGET_APP.bat` (it runs `scripts\start-target-access.ps1`; needs
  Docker Desktop with Kubernetes enabled, which ships `kubectl`). Leave the window open.
* **Browser:** <http://localhost:30081/health> and <http://localhost:30081/ready>.
* **Stop:** Ctrl+C in its window. Only the processes the launcher started are stopped.
* `START_TARGET_APP.bat -CheckOnly` only checks kubectl, the context, the service and the port.
* **From the dashboard:** the *Kubernetes target* card (Incidents and Chaos pages) has **Open Target
  App** (the app's `/docs` page; it has no page at `/`), **Health Check** and **Readiness Check**
  links, opening in a new tab, with the status Kubernetes reports. The base URL comes from
  `NEXT_PUBLIC_TARGET_APP_URL` when the dashboard is built or started (default
  `http://localhost:30081`).

It only reads from the cluster, and AutoFixOps does not use it: Prometheus scrapes the node IP and
recovery verification goes through the API server's service proxy. A page loading in the browser is
not a recovery verdict; the dashboard's verification is.

### How it stays connected

* **Kubernetes decides where.** The launcher forwards to the best pod behind the service (running,
  not terminating, ready first, newest first), found with the service's own selector and port
  mapping. It does not use `kubectl port-forward svc/...`: that picks one pod when it starts and stays
  on it, so it silently breaks as soon as chaos or a rollout replaces the pod.
* **HTTP decides whether the app is healthy.** `/health` and `/ready` are requested through the
  tunnel every 5 s. A running kubectl process is never taken as health.
* **The tunnel is reopened** when kubectl exits, when its pod is deleted or terminating, when a ready
  pod exists while the forwarded one is not ready, and when checks get no answer although Kubernetes
  reports the pod ready (a tunnel that is alive but unusable).
* **kubectl ends a port-forward as soon as the app refuses one connection** (crash, OOM restart,
  start-up). During such an outage the launcher does not probe, reopens the tunnel quietly with a
  growing delay (up to 10 s), and reopens it at once when the pod becomes ready again.

The launcher cannot refresh a browser tab: after **BROWSER ACCESS RESTORED**, refresh it.

| Status | Meaning |
|---|---|
| `STARTING` | Checks passed; watching the pods behind the service. |
| `WAITING FOR KUBERNETES` | The cluster API is unreachable. An open tunnel is kept. |
| `WAITING FOR READY POD` | No running pod behind the service (pod being created or replaced). |
| `PORT-FORWARD CONNECTED` | The tunnel is open to the named pod. Not a health statement. |
| `APPLICATION HEALTHY` | `/health` and `/ready` both answered HTTP 200 through the tunnel. |
| `APPLICATION NOT READY` | The app answers, but `/ready` fails (for example 503 `overloaded` during CPU Spike). |
| `APPLICATION DOWN` | The container is not running, or `/health` fails or does not answer. |
| `CHAOS OUTAGE DETECTED` | The outage coincides with a fault in ConfigMap `target-app-chaos`: expected; keep waiting. |
| `POD REPLACED` | The forwarded pod was deleted, is terminating, or a ready pod replaced a not-ready one. |
| `RECONNECTING` | The tunnel itself failed (kubectl exited or the tunnel stopped answering) and is reopened. |
| `BROWSER ACCESS RESTORED` | Healthy again through the tunnel after an outage. Refresh the tab. |
| `STOPPED` | Ctrl+C: the tunnel is closed and the port released. |

A healthy line during active chaos says "Chaos is still configured": the app answers now, but the
fault may strike again (a leak is OOM-killed every few minutes until the fix is deployed).

### What you see in each scenario

* **Memory Leak:** injection replaces the pod (`POD REPLACED`, then `BROWSER ACCESS RESTORED` on the
  new pod). Memory grows until the container is OOM-killed and restarts in the same pod:
  `CHAOS OUTAGE DETECTED` / `APPLICATION DOWN`, then healthy again for a few minutes, repeatedly. The
  remediation (`RESPONSE_CACHE_ENABLED=false`) rolls out a new pod: `POD REPLACED`,
  `BROWSER ACCESS RESTORED`.
* **CPU Spike:** the new pod stays Running but becomes not ready (`/ready` 503 `overloaded`):
  `CHAOS OUTAGE DETECTED` / `APPLICATION NOT READY`. The tunnel stays open, so the browser shows the
  503. The remediation (larger CPU limit) rolls out a pod that becomes ready while the old one is
  still not ready: the launcher moves to it (`POD REPLACED`) and reports `BROWSER ACCESS RESTORED`.
* **Crash Loop:** the container dies at start-up over and over (CrashLoopBackOff); there may be no
  ready pod for minutes. `CHAOS OUTAGE DETECTED` / `APPLICATION DOWN`; the browser cannot connect.
  The launcher keeps running quietly. After the remediation (larger memory limit) it waits for the
  new pod (`WAITING FOR READY POD` while it is created) and reports `BROWSER ACCESS RESTORED`.

### Demo workflow

1. Start Docker Desktop with Kubernetes enabled.
2. Start the AutoFixOps API, Celery worker, Prometheus/Alertmanager (`docker compose up -d`) and the dashboard.
3. Double-click `START_TARGET_APP.bat`; wait for `APPLICATION HEALTHY`. Keep the window open.
4. Open <http://localhost:30081/health> (HTTP 200, `{"status":"ok"}`).
5. Inject Memory Leak, CPU Spike or Crash Loop from the dashboard's **Chaos** page.
6. Refresh the tab: the target becomes unavailable (Memory Leak, Crash Loop) or `/ready` returns 503
   (CPU Spike). The launcher window says which.
7. Let AutoFixOps diagnose the incident.
8. Approve the remediation in the dashboard.
9. Wait for the GitHub pull request and its CI checks.
10. Merge the pull request manually on GitHub.
11. Wait for the Kubernetes deployment and the recovery verification in the dashboard. The launcher
    follows the new pod by itself and prints `BROWSER ACCESS RESTORED`.
12. Refresh the target app's browser tab.
13. Confirm HTTP 200 and the healthy response, and that the dashboard reports the recovery as verified.

### Troubleshooting

* **"Port 30081 is already in use"** names the program. If it is another launcher, use that window.
  If it is a hand-started `kubectl port-forward`, close it: `svc/...` forwards break when the pod is
  replaced.
* **"context 'docker-desktop' does not exist" / "cluster ... is not reachable":** start Docker
  Desktop and enable Kubernetes. **"Service ... was not found":** `python scripts/k8s_demo.py setup`.
* **The browser shows an error but the window says `APPLICATION HEALTHY`:** refresh the tab; try
  <http://127.0.0.1:30081/health> if `localhost` misbehaves (the tunnel listens on IPv4 only).
* **The window stopped updating:** the launcher turns off QuickEdit so a click cannot pause it; in
  other terminals, press Esc if text is selected.
* Mocked end-to-end checks of the launcher (Windows): `python tests/browser_access/run_launcher_scenarios.py`.

## Security and limitations

* **Local demonstration only.** Approvals are recorded as unauthenticated and accepted
  only from this machine unless `APPROVER_TOKENS` is configured. The rest of the API
  (Stage 2 generation approval, kill switch, chaos) has no authentication. Do not
  expose the API publicly without proper authentication and role-based authorization.
* The LLM never writes YAML, commands or paths: it only picks one value from a menu
  computed by code, and every proposal must pass all validation checks.
* Deployment is **off by default** and limited to explicitly allowlisted targets in a
  named kubectl context; protected namespaces are always refused.
* Only memory/CPU limit increases are automated. Other actions are escalated to a human.
* The local target app (Windows process) has no real memory limit; real enforcement and
  rollout happen only in the Kubernetes demo.
* Credentials come from `.env` or the encrypted service-account record; never commit them.

## Repository layout

- `api/` — FastAPI gateway (webhooks, incidents, Stage 2 and Stage 3–4 endpoints)
- `workers/` — Celery pipeline tasks
- `engine/` — telemetry, diagnosis, RAG, proposer and safety checks, remediation workflow, deployer, verifier
- `dashboard/` — Next.js dashboard
- `monitoring/` — Prometheus, alert rules, Alertmanager, scrape target file
- `kubernetes_integration/` — target app, its manifests and the in-cluster monitoring stack
- `scripts/` — schema migration, runbook seeding, proposer CLI, Kubernetes demo helper, browser-access launcher (`START_TARGET_APP.bat`)
- `tests/` — offline test suites (see the runbook for the list)
- Docs: [USER_GUIDE.md](USER_GUIDE.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
  [docs/RUNNING_ON_WINDOWS.md](docs/RUNNING_ON_WINDOWS.md), [docs/RAG.md](docs/RAG.md),
  [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md) (original design)
