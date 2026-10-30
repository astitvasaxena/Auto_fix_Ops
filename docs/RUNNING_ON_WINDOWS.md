# Running AutoFixOps locally on Windows

All commands are for **PowerShell** in the repository root (`C:\Users\<you>\Auto_fix_Ops`).
Each long-running process gets its own terminal.

| Service | Port | Started by |
|---|---|---|
| AutoFixOps API | 8010 | uvicorn (terminal 2) |
| Dashboard | 3000 | `npm run dev` (terminal 4) |
| Target app (local process) | 8001 | uvicorn (terminal 1) |
| Target app (Kubernetes demo) | 30081 | `scripts/k8s_demo.py setup` |
| Prometheus / Alertmanager | 9090 / 9093 | docker compose |
| Postgres / Redis / Qdrant | 5433 / 6379 / 6333 | docker compose |
| Ollama (`qwen2.5:3b`) | 11434 | Ollama app |

## 1. One-time setup

```powershell
# Prerequisites: Python 3.12, Node 22, Docker Desktop, Git, Ollama (https://ollama.com)
ollama pull qwen2.5:3b
python -m pip install -r requirements.txt
cd dashboard; npm install; cd ..
Copy-Item .env.example .env        # then edit .env (see section 4); never commit .env
docker compose up -d               # postgres, redis, prometheus, alertmanager, qdrant
python scripts/migrate_schema.py   # idempotent schema migration
$env:QDRANT_URL = "http://localhost:6333"; python scripts/seed_rag_runbooks.py   # curated runbooks
```

Minimum `.env` for the local pipeline:

```
POSTGRES_URL=postgresql://autofixops:autofixops_password@localhost:5433/autofixops   # docker-compose.yml defaults
REDIS_URL=redis://localhost:6379/0
PROMETHEUS_URL=http://localhost:9090
QDRANT_URL=http://localhost:6333
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:3b
```

## 2. Start everything (four terminals)

```powershell
# Terminal 1 - target app (local process; skip this when using the Kubernetes demo)
python -m uvicorn kubernetes_integration.target_app.app:app --host 0.0.0.0 --port 8001

# Terminal 2 - API
python -m uvicorn api.main:app --host 0.0.0.0 --port 8010

# Terminal 3 - Celery worker (--pool=solo is required on Windows)
python -m celery -A workers.celery_app worker --loglevel=info --pool=solo

# Terminal 4 - dashboard
cd dashboard; npm run dev          # http://localhost:3000
```

Restart the API and the worker after changing `.env`. After changing
`monitoring/alertmanager.yml` or `alert_rules.yml` reload them:
`Invoke-WebRequest -Method Post http://localhost:9093/-/reload` and
`Invoke-WebRequest -Method Post http://localhost:9090/-/reload`.

## 3. Stage 2 demo (works without Kubernetes or GitHub)

```powershell
1..4 | ForEach-Object { Invoke-RestMethod http://localhost:8001/leak }   # +40 MB, ~90% of the 100Mi limit
```

Within about a minute `HighMemoryUsage` fires (Prometheus → Alertmanager → API).
On the incident page you can watch: context (real memory peak), diagnosis with
runbooks, routing (≥ 0.80 automatic, otherwise **Approve AI Patch Generation**)
and the **VALIDATED** proposal with its diff and 16 checks.
`Invoke-RestMethod http://localhost:8001/recover` frees the memory; the alert clears.

## 4. Full self-healing demo (Stages 3–4) on a disposable local cluster

1. **Enable Kubernetes**: Docker Desktop → Settings → Kubernetes → *Enable Kubernetes* → Apply.
   `kubectl config get-contexts` must list `docker-desktop`.
2. **Create the demo environment** (builds the image, applies the namespace, the
   repository's `deployment.yaml` with a 100Mi limit and a NodePort service, and
   points Prometheus at the pod):
   ```powershell
   python scripts/k8s_demo.py setup
   python scripts/k8s_demo.py status
   ```
   Stop the local target app (terminal 1) so only the pod is monitored.
3. **Configure `.env`** and restart the API and worker:
   ```
   SHADOW_MODE=false                       # shadow mode never deploys
   DEPLOY_MODE=kubectl
   DEPLOY_KUBE_CONTEXT=docker-desktop
   DEPLOY_ALLOWED_TARGETS=autofixops/autofixops-target-app
   VERIFY_HEALTH_URL=http://localhost:30081/health
   VERIFY_STABILIZATION_SECONDS=60
   # GitHub PR (recommended): a token with contents+pull-requests write on YOUR repo
   GITHUB_TOKEN=...                        # or set it on the dashboard Settings page (stored encrypted)
   GITHUB_REPO=<owner>/<repo>              # must be this checkout's origin
   # ...or a local-only run without a PR:
   # REMEDIATION_REQUIRE_PR=false
   # Optional authenticated approvals:
   # APPROVER_TOKENS=yourname:<a random string of 16+ characters>
   ```
   The dashboard system mode must be **ACTIVE** (not SHADOW / DISABLED).
4. **Inject the failure**:
   ```powershell
   Invoke-RestMethod http://localhost:30081/health          # healthy
   1..4 | ForEach-Object { Invoke-RestMethod http://localhost:30081/leak }
   ```
5. **Watch and approve** on `http://localhost:3000/incidents`:
   alert → context → diagnosis → VALIDATED proposal → click **Approve remediation**.
6. **GitHub**: AutoFixOps opens a PR containing exactly the validated diff. Review
   and **merge it on GitHub without editing it** (an edited merge is refused).
7. AutoFixOps sees the merge, patches the one limit with `kubectl` (dry run first),
   waits for the rollout and verifies: deployment ready, pods healthy, `/health`
   200, alert cleared and memory below 80% of the new limit, continuously for the
   stabilization window → incident **RESOLVED**. If verification fails it restores
   the previous limit and opens a revert PR.
8. **Reset** for another run: `git pull` (main now has the new limit), then
   `python scripts/k8s_demo.py setup` (re-applies the repository manifest), or
   `python scripts/k8s_demo.py teardown` to remove everything and point
   Prometheus back at port 8001.

## 5. Tests

```powershell
# Offline Python suite, same files and environment as CI (in-memory SQLite, no LLM, no GitHub)
$env:POSTGRES_URL="sqlite://"; $env:MONGO_URL=""; $env:QDRANT_URL=""; $env:GITHUB_TOKEN=""; $env:GITHUB_REPO=""
$env:REDIS_URL="redis://127.0.0.1:9/0"; $env:PROMETHEUS_URL="http://127.0.0.1:9"; $env:OLLAMA_BASE_URL="http://127.0.0.1:9"
$env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"; $env:PYTHONIOENCODING="utf-8"
python -m pytest -q tests/ai tests/rag/test_runbooks.py tests/remediation tests/test_pre_diagnosis_context_flow.py `
  tests/safety_drills tests/evaluator/test_rollback.py `
  --deselect tests/safety_drills/test_bad_patch.py::test_patch_does_not_mutate_original `
  --deselect tests/safety_drills/test_bad_patch.py::test_restart_annotation_format `
  --deselect tests/rag/test_runbooks.py::test_seed_script_replaces_legacy_and_is_idempotent

python tests/evaluator/harness.py                 # diagnosis accuracy (informational)
cd dashboard; npm test; npm run build; cd ..      # dashboard unit tests and production build
```

Test groups:

| Area | File |
|---|---|
| Telemetry collection | `tests/ai/test_telemetry.py` |
| Pre-diagnosis context + debug endpoints | `tests/test_pre_diagnosis_context_flow.py` |
| AI analysis / RAG | `tests/ai/test_incident_analysis.py`, `tests/rag/test_runbooks.py` |
| Patch safety (Stage 2) | `tests/ai/test_patch_proposals.py` |
| Confidence routing + approval (Stage 2) | `tests/ai/test_patch_routing.py` |
| Approval, GitHub PR, deployment, verification, rollback (Stages 3–4) | `tests/remediation/test_remediation_workflow.py` (fakes for GitHub, kubectl, Prometheus) |
| Dashboard view logic | `dashboard/tests/*.test.mjs` |

The two deselected `test_bad_patch.py` tests are known, pre-existing failures of
the legacy `PatchGenerator` and are tracked in CI as such.
