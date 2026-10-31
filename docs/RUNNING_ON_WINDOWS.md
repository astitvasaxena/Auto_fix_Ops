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

## 4. Full self-healing demo: Kubernetes crash loop → PR → deploy → verified recovery

**Prerequisites (once):**
1. Docker Desktop → Settings → Kubernetes → *Enable Kubernetes*. `kubectl config get-contexts` lists `docker-desktop`.
2. The branch with Stages 3–4 must be merged into `main` (remediation PRs need its CI fix, and the
   proposer's base file must equal `main`).
3. `.env` (then restart the API and the worker; run **one** worker only):
   ```
   GITHUB_TOKEN=...                      # contents, pull requests: write; actions: read
   GITHUB_REPO=<owner>/<repo>            # this checkout's origin
   SHADOW_MODE=false                     # shadow mode never deploys
   REMEDIATION_REQUIRE_PR=true
   REMEDIATION_REQUIRE_CI=true           # deploy only after GitHub Actions passed on the PR commit
   DEPLOY_MODE=kubectl
   DEPLOY_KUBE_CONTEXT=docker-desktop
   DEPLOY_ALLOWED_TARGETS=autofixops/autofixops-target-app
   VERIFY_HEALTH_URL=kube-service://autofixops/target-app-nodeport:80/health
   VERIFY_STABILIZATION_SECONDS=60
   CHAOS_K8S_ENABLED=true                # crash-loop button on the dashboard Chaos page
   ```
   The dashboard system mode must be **ACTIVE**.

**Create the target in the cluster:**
```powershell
python scripts/k8s_demo.py setup     # image, namespace, chaos ConfigMap, deployment (100Mi), NodePort service
python scripts/k8s_demo.py status
```
*Docker Desktop's kind-based cluster* (node `desktop-control-plane`) cannot see images built with
`docker build`. Import the image once into the node (a temporary privileged debug pod, deleted afterwards):
```powershell
kubectl --context docker-desktop debug node/desktop-control-plane --image=busybox:1.36 --profile=sysadmin -- sleep 900
docker save autofixops/target-app:latest | kubectl --context docker-desktop exec -i <node-debugger-pod> -- chroot /host ctr -n k8s.io images import -
kubectl --context docker-desktop delete pod <node-debugger-pod>
python scripts/k8s_demo.py setup     # again, now the pod starts
```
The kind node does not publish NodePorts on Windows either: `setup` attaches the Prometheus
container to the `kind` Docker network and scrapes the node IP, and health checks go through the
API server's service proxy (`kube-service://...`).

**Demonstration (acceptance test):**
1. Dashboard → **Incidents**: the *Kubernetes target* card shows **HEALTHY** (1/1 ready, 0 restarts, 100Mi).
2. Dashboard → **Chaos** → *Inject crash loop* (or `python scripts/k8s_demo.py chaos crashloop`). The app
   now needs ~110% of its memory limit at start-up (ConfigMap `target-app-chaos`, survives pod replacement).
3. The pod becomes unhealthy: restarts climb; Kubernetes shows `OOMKilled`, or on nodes with swap
   (Docker Desktop) liveness-probe kills (`exit 137`) and `CrashLoopBackOff`.
4. Prometheus fires the memory/availability alerts; Alertmanager posts them; **one** incident is created.
5. The incident page shows real telemetry, Kubernetes evidence (restarts, termination, events, logs),
   runbooks, the Ollama diagnosis and the routing decision.
6. The proposer offers only limits that leave verification headroom (e.g. 150Mi/200Mi for a ~110Mi need)
   and stores a **VALIDATED** diff with all checks.
7. Click **Approve remediation** (the exact proposal). AutoFixOps opens a PR with that diff only.
8. Wait for **GitHub Actions** to pass on the PR, then **merge it on GitHub without editing it**.
9. AutoFixOps sees the merge (polls every `REMEDIATION_PR_POLL_SECONDS`), checks the merged file and CI,
   runs a server dry run, patches the one limit, waits for the rollout (new pod replaces the crashing one),
   then verifies deployment, pods, health, alert and headroom for the stabilization window → **RESOLVED**.
   If verification fails it restores the previous limit, opens a revert PR and marks the incident FAILED.

**The three chaos faults (dashboard Chaos page, Kubernetes mode, or `k8s_demo.py chaos ...`).**
Each one is a value in the ConfigMap `target-app-chaos` that the target app reads at start-up, plus
an expiry (`CHAOS_TTL_MINUTES`, default 30); injection restarts the pod, so the fault is real and
persists across restarts until it expires or is reset. The page never creates an incident: only
Prometheus/Alertmanager do.

| Fault | What happens in the pod | Detected by | What AutoFixOps does |
|---|---|---|---|
| Crash Loop (`crashloop`) | start-up needs the whole memory limit: OOMKilled at every start, CrashLoopBackOff, target down | TargetAppDown | memory limit increase (2x, the largest bounded value) after approval, PR, CI and merge; verified recovery |
| Memory Leak (`memoryleak`) | memory grows 40 MB/min until OOMKilled, again after every restart | TargetAppMemoryLeak / HighMemoryUsage | refuses a larger limit (code-measured growth ⇒ LEAK_SUSPECTED: it would only delay the next kill) and leaves the incident to a human; recover with `chaos reset` |
| CPU Spike (`cpuspike`) | CPU demand 140% of the CPU limit: pinned at the limit and throttled (degraded, not down) | HighCPUUsage (usage > 90% of the CPU limit for 1 min) | CPU limit increase (2x, real demand is hidden by throttling) after approval, PR, CI and merge; verified recovery |

The CPU and leak faults, the expiry and the CPU-limit metric need the current target image and the
HighCPUUsage rule: rebuild/import the image (see above) and reload Prometheus
(`Invoke-WebRequest -Method Post http://localhost:9090/-/reload`).

A remediation whose deployment or verification fails is rolled back and the incident is
**ESCALATED** to a human (close it with a reason on the incident page once handled).

**Reset for another run:** `git pull` (main now has the new limit; it must equal your working tree),
`python scripts/k8s_demo.py chaos reset`, and to remove everything `python scripts/k8s_demo.py teardown`.

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
