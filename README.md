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
| 4 | Controlled `kubectl` deployment (allowlist, dry run, drift-safe JSON patch, lock), recovery verification with stabilization window, rollback + revert PR | Implemented and tested with a fake cluster; a live run needs Docker Desktop Kubernetes |
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
