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
`http://localhost:30081` refuses connections on its own. Double-click **`START_TARGET_APP.bat`**
(or run `scripts\start-target-access.ps1`) to open a `kubectl port-forward` from
`127.0.0.1:30081` to service `autofixops/target-app-nodeport` port 80 (context `docker-desktop`):

* <http://localhost:30081/health> and <http://localhost:30081/ready>
* It checks kubectl, the context, the service and that port 30081 is free (and names the program
  holding it if not), then keeps the tunnel alive: it reconnects when kubectl drops the connection
  and when the pod is replaced (chaos, rollout, remediation). Ctrl+C stops it.
* It only reads from the cluster. AutoFixOps does not use it: Prometheus scrapes the node IP and
  recovery verification goes through the API server's service proxy, so the tunnel cannot affect
  detection or verification. A page loading in the browser is not a recovery verdict; the
  dashboard's verification result is.
* `START_TARGET_APP.bat -CheckOnly` runs only the checks.

**Live demo:**
1. Start Docker Desktop with Kubernetes enabled.
2. Start the AutoFixOps API, Celery worker, Prometheus/Alertmanager (`docker compose up -d`) and the dashboard.
3. Double-click `START_TARGET_APP.bat` and leave its window open.
4. Open <http://localhost:30081/health> in the browser (HTTP 200, `{"status":"ok"}`).
5. Inject a chaos fault from the dashboard (**Chaos** page).
6. Refresh the tab: the target becomes unavailable or unready (`/ready` returns 503, or the
   connection fails while the container restarts). The tunnel window says which.
7. Let AutoFixOps diagnose the incident.
8. Approve the remediation in the dashboard.
9. Wait for the GitHub pull request and its CI checks.
10. Merge the pull request manually on GitHub.
11. Wait for the Kubernetes deployment and the recovery verification in the dashboard. The tunnel
    reconnects to the new pod by itself ("reconnected: ... (refresh the browser)").
12. Refresh the target app's browser tab.
13. Confirm HTTP 200 and the healthy response, and that the dashboard reports the recovery as verified.

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
- `scripts/` — schema migration, runbook seeding, proposer CLI, Kubernetes demo helper
- `tests/` — offline test suites (see the runbook for the list)
- Docs: [USER_GUIDE.md](USER_GUIDE.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
  [docs/RUNNING_ON_WINDOWS.md](docs/RUNNING_ON_WINDOWS.md), [docs/RAG.md](docs/RAG.md),
  [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md) (original design)
