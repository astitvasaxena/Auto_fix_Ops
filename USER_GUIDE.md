# AutoFixOps — User Guide

> A self-healing operations assistant for Kubernetes-based applications.
> When something goes wrong with your app, AutoFixOps notices, figures out
> what's wrong, proposes a fix as a Pull Request on GitHub, and (after you
> merge it) verifies that the fix actually worked.

This guide is written for an **end user** who wants to install AutoFixOps,
connect their own GitHub repository, and see real fixes flow through the
dashboard. No prior knowledge of Celery, Qdrant, or Kubernetes internals
is assumed.

---

## Table of Contents
1. [What AutoFixOps Does (in one paragraph)](#1-what-autofixops-does-in-one-paragraph)
2. [Who It Is For](#2-who-it-is-for)
3. [What Kinds of Apps It Can Watch](#3-what-kinds-of-apps-it-can-watch)
4. [The Five Things It Does (Features)](#4-the-five-things-it-does-features)
5. [Scope and Limitations](#5-scope-and-limitations-the-honest-list)
6. [How a Fix Actually Happens (the user's eye view)](#6-how-a-fix-actually-happens-the-users-eye-view)
7. [Setup — Installing AutoFixOps](#7-setup--installing-autofixops)
8. [Connecting Your GitHub Account (so it can open PRs)](#8-connecting-your-github-account-so-it-can-open-prs)
9. [The Dashboard — Page by Page](#9-the-dashboard--page-by-page)
10. [The CLI / API — Everything the Dashboard Does, Without the UI](#10-the-cli--api--everything-the-dashboard-does-without-the-ui)
11. [Safety Features (and why they exist)](#11-safety-features-and-why-they-exist)
12. [Tradeoffs You Should Know About](#12-tradeoffs-you-should-know-about)
13. [Troubleshooting](#13-troubleshooting)
14. [FAQ](#14-faq)

---

## 1. What AutoFixOps Does (in one paragraph)

You run a service somewhere (typically on Kubernetes). Your monitoring
system (Prometheus + Alertmanager) is configured to send an alert when
something is unhealthy — high memory, CPU spike, crash loop, etc.
Normally a human gets paged, opens dashboards, figures out the cause,
edits a YAML file, and creates a Pull Request. AutoFixOps replaces the
boring middle steps. It receives the alert, gathers metrics, classifies
the problem, decides if it's safe to act, generates the YAML change,
opens a real Pull Request on **your** GitHub repo for **you** to
review and merge, and then watches whether the fix actually helped.

It is **not** an autopilot. **You** still merge the PR. AutoFixOps is
the assistant that does everything up to that moment so you can spend
your time on the actual decision.

---

## 2. Who It Is For

AutoFixOps is for someone who already runs an app where:
- Alerts are fired by Prometheus / Alertmanager
- The deployment configuration lives in a Git repository (GitOps style:
  the YAML in Git is the source of truth, and Argo CD / Flux / a CI
  job applies it)
- You — or your team — want suggestions and ready-to-merge fixes,
  not a black-box auto-pilot

If you operate a service without GitOps, you can still use AutoFixOps
in **shadow mode** (it diagnoses and proposes, but the proposed PR is
purely advisory).

---

## 3. What Kinds of Apps It Can Watch

| App style | Works? | Notes |
|---|---|---|
| Kubernetes Deployment with `resources.limits` | **Yes — first-class** | All built-in fixes target this layout. |
| Kubernetes StatefulSet | **Yes** | Same patch shapes work. |
| Helm chart that emits standard Deployments | **Yes** | Patches the rendered manifest in the repo. |
| Plain Docker-Compose service | Partial | Diagnosis works; remediation needs a Compose-aware patch generator (not built in yet). |
| Serverless (Lambda, Cloud Run) | No | Out of scope — there's no pod / no resource limits to patch. |
| Anything that does not expose Prometheus metrics | No | The diagnosis layer needs metrics. |

**Built-in remediation actions today:**
- `INCREASE_MEMORY_LIMIT` — bump `resources.limits.memory` (capped at 2× by default)
- `INCREASE_CPU_LIMIT` — bump `resources.limits.cpu`
- `RESTART_POD` — add a `kubectl.kubernetes.io/restartedAt` annotation
- `ROLLBACK_DEPLOYMENT` — revert image tag to the previous value
- `ESCALATE` — give up gracefully and ping a human (always allowed)

Anything else (e.g. "scale to 5 replicas", "drain node X") is **not** in
scope. If the diagnosis engine cannot map an incident to one of the
above, the system escalates rather than guessing.

---

## 4. The Five Things It Does (Features)

### 4.1 Receives alerts

A standard Alertmanager webhook is enough:

```yaml
# alertmanager.yml (relevant excerpt)
receivers:
  - name: autofixops
    webhook_configs:
      - url: http://<your-autofixops-host>:8010/api/v1/alerts
        send_resolved: true
```

Both `firing` and `resolved` alerts are accepted. Duplicates are
de-duplicated for 120 seconds based on `alertname + namespace + pod +
container + severity`.

### 4.2 Gathers evidence

Once an alert lands, the system queries your Prometheus for the metrics
that matter for the alert's pod (CPU rate, memory working-set) and
attaches them to the incident. If Prometheus is unreachable, the
incident still progresses — diagnosis just runs on the alert metadata
alone.

### 4.3 Diagnoses with a two-tier engine

1. **Rule Engine first.** Cheap, deterministic, no LLM cost. Handles
   the common cases (memory leak, CPU saturation, crash loop) with
   100% confidence.
2. **AI fallback** *(only if the Rule Engine returns "unknown")*. The
   metrics get summarized into a small dictionary (peak, trend, tags)
   — never the raw 900-point arrays — and an LLM is asked to classify
   into one of a fixed set of categories. The LLM cannot return free
   text. If it returns anything outside the schema, or its confidence
   is below 0.80, the result is rejected and the incident is escalated.

### 4.4 Decides whether to act (Policy Gates)

Before the system does anything, the proposed action passes through
six gates in order:

1. **Kill switch** — is the system globally `DISABLED`?
2. **Action allowlist** — is the proposed action in the supported set?
3. **Confidence floor** — is the diagnosis confident enough?
4. **Namespace check** — is the target namespace allowed for autonomous action? (`prod`, `payments`, etc. require human approval by default.)
5. **Anti-thrashing** — has this action already been attempted ≥3 times for this incident in the last 60 minutes?
6. **Circuit breaker** — has more than 50% of recent attempts (per project, per action type) failed?

Any single gate failing routes the incident to **ESCALATED**, which
appears on the dashboard with a blue badge and a one-click "Approve"
button.

### 4.5 Opens a Pull Request and watches for the merge

If all gates pass, the system clones the **manifest path** in your
GitHub repo, modifies the relevant container's resource limits, opens
a draft PR in your repo (yes — your real repo), and writes the full
incident evidence into the PR body. It then polls the PR every 60s.
When you merge it, the system waits 5 minutes for the change to
propagate, queries Prometheus again, and confirms that the fix
actually moved the metric in the right direction — the **stability
window** check. Only then is the incident marked `RESOLVED`.

If the metrics get worse, the incident is marked `FAILED` and the
attempted action is recorded in the failure memory so future
recommendations can avoid it.

---

## 5. Scope and Limitations (the honest list)

| Out of scope | Why |
|---|---|
| Multi-cluster federation | One cluster per project, one project per AutoFixOps install. |
| Auto-merge | Intentional. The point of AutoFixOps is that **you** merge. |
| Unbounded kubectl write actions | Only Stage 4 writes to a cluster, and only one approved, validated resource limit, to an allowlisted Deployment, after a human approval and (by default) a merged PR. It is off unless `DEPLOY_MODE=kubectl`. |
| Database migrations as a remediation action | Too risky for autonomous diagnosis. |
| Diagnosing application logic bugs | We diagnose **infrastructure** symptoms (resources, lifecycle), not "this query is slow" or "this auth check is wrong". |
| Replacing your on-call engineer | This is an assistant. A human still owns the incident. |

**Known limits today:**
- Single project / single GitHub repo per install (multi-tenant data
  model is in place but the dashboard exposes one project).
- Prometheus is the only telemetry source. Datadog / New Relic /
  CloudWatch are not built in.
- The LLM call costs go to whatever provider you point `OPENAI_API_BASE` at.
- **Local demonstration only: the API has no authentication or authorization.**
  Anyone who can reach the API can approve or reject AI patch generation
  (Stage 2), approve escalations and change the kill switch. Stage 2
  approvals are recorded as `authenticated: false` with the client address
  and user agent only — no user identity is invented. Run the API on
  `localhost` or a private network, and **do not deploy it publicly** until
  authentication and role-based authorization are added.
- **Stage 3–4 approvals** (approve / reject / close / retry a remediation) are
  stricter: with `APPROVER_TOKENS=name:token` set they require that bearer token
  and record `name` as the authenticated approver; without it they are accepted
  **only from this machine** (loopback) and recorded as unauthenticated.
- Only memory/CPU limit increases are automated; other actions are escalated.
- The local target app (a Windows process) has no real memory limit. Real
  enforcement, rollout and verification need the Kubernetes demo
  (`scripts/k8s_demo.py`, see `docs/RUNNING_ON_WINDOWS.md`).

---

## 6. How a Fix Actually Happens (the user's eye view)

A real example, from the perspective of someone watching the dashboard:

1. **08:14:02** — A red banner appears on the **Incidents** page:
   `TargetAppMemoryLeak — critical — namespace: autofixops`.
2. **08:14:05** — Status flips from `INGESTED` to `CONTEXT_BUILT`. A
   small badge shows the CPU and memory snapshots that were collected.
3. **08:14:07** — Status flips to `DIAGNOSED`. The badge now reads
   `MEMORY_LEAK_OOM_RISK · RULE_ENGINE · 100%`.
4. **08:14:13** — Status flips to `POLICY_APPROVED`. Click into the
   incident and you see the six gates with green checkmarks.
5. **08:14:30** — Status flips to `PENDING_PR_MERGE`. A clickable link
   appears: **"PR #1 → vanshitahujaa/Auto_fix_Ops"**. The dashboard
   also shows the exact YAML diff (`memory: 100Mi → 200Mi`) and the
   rollback values that were stored.
6. **08:18:00** — You read the PR, agree, click Merge.
7. **08:23:00** — AutoFixOps polls Prometheus, sees memory has
   stabilized, marks the incident `RESOLVED`.
8. **08:23:05** — A green "✓ Verified stable" tag appears on the
   incident. The successful resolution is added to the RAG memory
   so similar future incidents resolve faster.

If at step 6 you reject the PR instead, the incident is marked
`PR_REJECTED` and never escalates further. If at step 7 the metrics
got *worse*, the incident is marked `FAILED` and an alert is logged
into the failure memory.

---

## 7. Setup — Installing AutoFixOps

### 7.1 Prerequisites

You will need:
- **macOS or Linux** with Python 3.9+
- **Docker Desktop** (for Redis only — everything else can be cloud-hosted)
- **Node 18+** (for the dashboard)
- **A Postgres database** — local or any cloud (NeonDB free tier works)
- **A MongoDB database** *(optional)* — local or Atlas. If unreachable,
  AutoFixOps falls back to in-memory storage automatically.
- **A GitHub Personal Access Token** with `repo` scope ([create one](https://github.com/settings/tokens))
- **A target Git repo** that contains your Kubernetes deployment YAML

### 7.2 Clone and install

```bash
git clone https://github.com/vanshitahujaa/Auto_fix_Ops.git
cd Auto_fix_Ops

# Backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Dashboard
cd dashboard && npm install && cd ..
```

### 7.3 Configure `.env`

Copy `.env.example` to `.env` and fill in:

```bash
# Database
POSTGRES_URL=postgresql://user:pass@host:5432/dbname
MONGO_URL=mongodb+srv://user:pass@host/      # optional

# GitHub (so PRs can be opened on your repo)
GITHUB_TOKEN=ghp_your_personal_access_token
GITHUB_REPO=your-username/your-repo

# Encryption key for at-rest token storage (any random string)
ENCRYPTION_KEY=change-me-in-production

# Prometheus endpoint (your real one)
PROMETHEUS_URL=http://your-prometheus:9090

# Redis (local docker container)
REDIS_URL=redis://localhost:6379/0

# Where in your repo the deployment YAML lives
TARGET_MANIFEST_PATH=k8s/production/deployment.yaml

# Shadow mode (true = PRs are drafts and never auto-mergeable)
SHADOW_MODE=true

# Optional: AI fallback
OPENAI_API_KEY=sk-...
OPENAI_API_BASE=https://api.openai.com/v1   # or your custom gateway
OPENAI_MODEL_NAME=gpt-4o-mini
```

### 7.4 Bring up the system

```bash
# 1. Redis
docker compose up -d redis

# 2. Initialize / migrate the Postgres schema (idempotent)
./venv/bin/python scripts/migrate_schema.py

# 3. The API server (port 8010)
./venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8010

# 4. In another terminal, the worker
./venv/bin/celery -A workers.celery_app worker --loglevel=info --pool=solo

# 5. In a third terminal, the dashboard (port 3000)
cd dashboard && npm run dev
```

Open [http://localhost:3000](http://localhost:3000). You should see an
empty incident list and the system mode `ACTIVE` in the top bar.

---

## 8. Connecting Your GitHub Account (so it can open PRs)

You have two ways to give AutoFixOps your token. **Option A is easier
for getting started; Option B is the safer long-term path.**

### Option A — Through the dashboard (Settings page)

1. Open [http://localhost:3000/onboard](http://localhost:3000/onboard)
2. Fill in:
   - **Project name** — anything, e.g. "production-api"
   - **GitHub Repository** — `your-username/your-repo`
   - **GitHub Token** — paste the token you generated
   - **Target manifest path** — e.g. `k8s/production/deployment.yaml`
   - **Prometheus URL** — your Prometheus
   - **Allowed chaos namespaces** — e.g. `staging,test`
   - **Max resource scale factor** — `2.0` (recommended)
3. Click **Save**.

The token is encrypted with Fernet (AES-128 in CBC mode) using your
`ENCRYPTION_KEY` before being written to Postgres. When the dashboard
re-reads it, it shows only the masked form (`ghp_****abcd`). The
plaintext is decrypted only inside the worker process at the moment
it talks to the GitHub API.

### Option B — Through the `.env` file

If you prefer config-as-code, just set `GITHUB_TOKEN` and
`GITHUB_REPO` in `.env`. The system uses the env values when no
`ProjectConfig` row is present. **Caveat:** env vars are static —
restart needed to change them; the DB-backed config is hot-reloaded
every 60 seconds.

### Verifying it works

Trigger a test incident:

```bash
curl -X POST http://localhost:8010/api/v1/alerts \
  -H 'Content-Type: application/json' \
  -d '{
    "receiver":"autofixops","status":"firing",
    "alerts":[{"status":"firing",
      "labels":{"alertname":"TargetAppMemoryLeak","severity":"critical",
                "namespace":"autofixops","pod":"smoke-test","container":"target-app"},
      "annotations":{"summary":"smoke test"},
      "startsAt":"2026-04-30T13:00:00Z","endsAt":"0001-01-01T00:00:00Z"}],
    "groupLabels":{},"commonLabels":{},"commonAnnotations":{},
    "externalURL":"http://am","version":"4","groupKey":"smoke"}'
```

Within ~30 seconds, you should see a real PR appear on the GitHub repo
you configured. If you don't, check the **Troubleshooting** section.

---

## 9. The Dashboard — Page by Page

All pages are under [http://localhost:3000](http://localhost:3000).

### Top bar (always visible)
- **System Mode dropdown** — `ACTIVE` / `SHADOW` / `DISABLED`. Setting
  `DISABLED` is the kill switch; every Celery task aborts at the top.
- **Circuit Breaker status** — `CLOSED` (normal) / `OPEN` (system has
  paused autonomous action because too many recent attempts failed).
- **GitHub connectivity pill** — green if the configured token can list
  the configured repo.

### `/incidents` — the live incident list
The home page. Real-time table (WebSocket-pushed; falls back to 5-second
polling if the WS drops).

| Column | Meaning |
|---|---|
| ID | Short incident identifier — click to drill in |
| Status | One of: `INGESTED → CONTEXT_BUILT → DIAGNOSED → POLICY_APPROVED → REMEDIATING → PENDING_PR_MERGE → RESOLVED` (or `ESCALATED` / `FAILED`) |
| Alert | The original Prometheus alert name |
| Severity | `info` / `warning` / `critical` |
| Diagnosis | What the system thinks is wrong |
| Engine | `RULE_ENGINE` or `AI_ENGINE` |
| Confidence | 0.0–1.0 |
| Created | When it landed |

Top-right filter dropdown: **All / Active / Resolved / Escalated**.

### `/incidents/{id}` — incident detail
Five sections, top to bottom:

1. **Pipeline** — visual status timeline with the timestamp of each transition.
2. **Resolved Target** — the exact `namespace / deployment / container`
   that the resolver picked, and a confidence score.
3. **Policy Gate Breakdown** — green / red list of all six gates with
   the reason recorded for each. (Kill switch, allowlist, escalation,
   confidence floor, namespace, anti-thrash.)
4. **PR Card** — the GitHub PR URL, the YAML diff that was applied,
   and the **Rollback** button.
5. **Audit timeline** — every state transition with timestamps, what
   the AI said (if invoked), what the policy decided, every retry.

The **Rollback** button takes the `previous_values` recorded at fix
time and creates a *second* PR that reverts only those exact fields.
Same review-and-merge flow.

**AI Remediation panel (Stage 2 — proposal only).** For an incident
diagnosed by the AI analyst, the backend routes AI patch generation by the
stored diagnosis confidence (one fixed threshold of **0.80**):

| Confidence | Route | What happens |
|---|---|---|
| ≥ 0.80 (e.g. 0.8000, 0.8001) | `AUTOMATIC_AI` | A proposal is generated without asking. |
| < 0.80 (e.g. 0.7999) | `HUMAN_APPROVAL_REQUIRED` | Nothing is generated until someone clicks **Approve AI Patch Generation**; **Reject Recommendation** means never for that diagnosis. |

Both routes run the same proposer: the local model may only pick a value
from a code-generated menu (or decline), and Python builds the diff and
validates it in a temporary sandbox. Only a `VALIDATED` proposal shows a
diff. Stage 2 itself applies, commits, pushes and deploys nothing — that
is the job of the panel below it (Stages 3–4).
The model is consulted at most once per diagnosis version on every path
(worker, API retry, CLI): once it has decided (`VALIDATED`, `REJECTED` or a
decline), later runs return `ALREADY_GENERATED`. A `FAILED` run, a refusal
caused by the kill switch or configuration, or a queued job that went stale
can be retried; a new diagnosis is a new version.
A `PATCH_PROPOSAL_MIN_CONFIDENCE` or project `confidence_threshold` other
than 0.80 is refused as `CONFIG_INVALID`. Set `PATCH_PROPOSALS_AUTO=false`
to stop automatic generation (the panel then shows a Generate button).
Approvals are unauthenticated — see the local-demo limitation in section 5.

**Approval, Pull Request, Deployment & Verification panel (Stages 3–4).**
Shown under the Stage 2 panel. Every remediation needs a human decision on the
*exact* validated proposal, whatever the confidence:

1. **Approve remediation** (or **Reject**, which needs a reason). Only the
   incident's latest, integrity-checked `VALIDATED` proposal built from the
   current diagnosis can be approved; the kill switch refuses approvals.
2. **Pull request** — AutoFixOps creates branch `autofixops/remediation-<id>`,
   commits the file rebuilt from the stored diff (hash-checked against the
   proposal) and opens a PR whose description lists the incident, diagnosis,
   evidence, diff, validation results and approval. Shadow mode makes it a draft.
   Repeated clicks or retries reuse the same PR.
3. **Merge** — you merge on GitHub. A merge whose file differs from the approved
   patch is refused (never deployed).
4. **Controlled deployment** — `kubectl` patches the one limit of the allowlisted
   Deployment (server dry run first; the patch is refused atomically if the live
   value is not the one the proposal saw). The previous container spec is recorded.
5. **Recovery verification** — every `VERIFY_INTERVAL_SECONDS` the deployment,
   pods, `VERIFY_HEALTH_URL`, the incident's alert and memory/CPU headroom are
   checked; all must stay green for `VERIFY_STABILIZATION_SECONDS` →
   **RESOLVED**. Otherwise the previous value is restored and a revert PR is opened
   → incident **FAILED**.

Steps that cannot run yet (no GitHub credentials, deployment disabled, shadow
mode, target not allowlisted, cluster unreachable) show **Waiting: <reason>**
with what to configure, change nothing, and can be retried with **Retry this
step**. **Close incident** records a manual resolution with a reason when no
automated remediation is wanted. The panel ends with the full incident timeline
(alert, status changes, proposal, approval, PR, merge, deployment, verification).
The pipeline strip at the top marks a stage completed only when the backend has
recorded it (chaos-created incidents show Prometheus/AlertManager as skipped).

### `/metrics` — pipeline health
- **Cards:** total incidents, resolved, escalated, failed.
- **Engine split** (pie chart) — how many were caught by the rule
  engine vs. the AI fallback. Healthy systems have most incidents in
  the rule engine.
- **Policy decisions** (bar chart) — APPROVED vs. ESCALATED vs.
  REJECTED.
- **Temporal toggles** — `All time / 24h / 7d`.
- **Grafana iframe** — a panel for the operational metrics of
  AutoFixOps itself.

### `/chaos` — manual fault injection
A red, dangerous-looking page that exists to **test** AutoFixOps in a
non-production cluster. It calls the `/leak`, `/cpu`, or `/recover`
endpoints on the configured target app, simulating a fault. Three
guardrails:
- **Namespace allowlist** — refuses to fire if the target namespace
  isn't in the project's allowlist.
- **Type-CONFIRM** — you have to type the literal word `CONFIRM`.
- **60-second cooldown** per project, server-enforced. The button
  greys out with a countdown.

### `/onboard` — settings wizard
Where you configure (or reconfigure) the project: GitHub repo, token,
Prometheus URL, target manifest path, chaos namespaces, scale factor,
system mode buttons.

---

## 10. The CLI / API — Everything the Dashboard Does, Without the UI

Every dashboard action is a thin layer over a REST endpoint. If you'd
rather script it, here's the full surface.

### Ingest an alert
```bash
POST /api/v1/alerts
Content-Type: application/json
# Body: standard Alertmanager webhook payload
```

### List incidents
```bash
GET /api/v1/incidents
GET /api/v1/incidents?status=ESCALATED
GET /api/v1/incidents?status=PENDING_PR_MERGE
```

### Inspect one incident
```bash
GET /api/v1/incidents/{incident_id}/context
# Returns: full evidence chain — alert, telemetry summary, diagnosis,
# policy verdict, all audits, all timeline entries.
```

### Approve an escalated incident (human-in-the-loop)
```bash
POST /api/v1/escalations/{incident_id}/approve
# Resumes the pipeline at REMEDIATION.
```

### AI patch proposals (Stage 2 — proposal only, never applied)
```bash
GET  /api/v1/incidents/{incident_id}/patch-proposals          # route, decision, latest proposal (read-only)
POST /api/v1/incidents/{incident_id}/patch-proposals          # start (automatic route or approved); idempotent
POST /api/v1/incidents/{incident_id}/patch-proposals/approve  # low-confidence route only
POST /api/v1/incidents/{incident_id}/patch-proposals/reject
GET  /api/v1/patch-proposals/{proposal_id}                    # also /diff and /validation
# POST bodies must be JSON (Content-Type: application/json); confidence/route fields are refused (422).
# No authentication: local demonstration only — do not expose publicly without authN/authZ.
```
CLI: `python scripts/propose_patch.py <incident_id>` and `--show <incident_id>`.

### Remediation (Stages 3–4 — approval, PR, deployment, verification)
```bash
GET  /api/v1/incidents/{incident_id}/remediation           # run, config summary, timeline (read-only)
POST /api/v1/incidents/{incident_id}/remediation/approve   # {"proposal_id": "...", "reason": "..."}
POST /api/v1/incidents/{incident_id}/remediation/reject    # {"proposal_id": "...", "reason": "required"}
POST /api/v1/incidents/{incident_id}/remediation/advance   # retry / re-check the current step
POST /api/v1/incidents/{incident_id}/close                 # {"reason": "..."} manual resolution
# JSON only. With APPROVER_TOKENS set: header "Authorization: Bearer <token>"; otherwise loopback only.
```
Settings: `REMEDIATION_REQUIRE_PR`, `DEPLOY_MODE`, `DEPLOY_KUBE_CONTEXT`,
`DEPLOY_ALLOWED_TARGETS`, `VERIFY_HEALTH_URL`, `VERIFY_*_SECONDS` (see `.env.example`).

### Roll back a fix
```bash
POST /api/v1/incidents/{incident_id}/rollback
# Opens a revert PR using the previous_values recorded at fix time.
```

### Aggregate metrics
```bash
GET /api/v1/metrics                # all-time
GET /api/v1/metrics?window=24h
GET /api/v1/metrics?window=7d
```

### System mode (kill switch)
```bash
GET  /api/v1/system/mode
POST /api/v1/system/mode  -d '{"mode": "DISABLED", "reason": "weekend freeze"}'
POST /api/v1/system/mode  -d '{"mode": "SHADOW"}'
POST /api/v1/system/mode  -d '{"mode": "ACTIVE"}'
```

### Project config
```bash
GET  /api/v1/config       # token returned masked
POST /api/v1/config       # body: { name, github_repo, github_token, ... }
```

### Chaos injection (testing only)
```bash
POST /api/v1/chaos/inject
# Body: { "fault": "memory_leak" | "cpu_spike" | "recover",
#         "target_url": "http://your-target-app/" }
```

### System status (header pill data)
```bash
GET /api/v1/status
# Returns: { system_mode, circuit_breaker_state, github_connected }
```

### Real-time event stream
```bash
WS /api/v1/events/ws
# Server pushes: incident.created, incident.status_changed,
# incident.diagnosed, remediation.pr_created, remediation.verified.
```

### Check what's on the worker queue
```bash
docker exec autofixops-redis-1 redis-cli LLEN celery
```

### Tail the pipeline log for one incident
```bash
grep "TRACE:<incident_id>" celery.log
```

---

## 11. Safety Features (and why they exist)

| Feature | What it prevents |
|---|---|
| **Kill switch** (`DISABLED` mode) | One-click halt. Every task checks at the top. |
| **Shadow mode** | All PRs are draft and never auto-mergeable. Safe default for the first 30 days. |
| **Patch bounds** (default 2× cap, 4Gi/4CPU hard ceiling) | Stops a runaway "always increase memory" loop from exhausting the node. |
| **Previous-value capture** | Every patch records the value it overwrote, so rollback is exact. |
| **Anti-thrashing** | If the same action has been tried ≥3× for one incident in the last hour, the next attempt is blocked. |
| **Circuit breaker** (per project, per action type) | If recent attempts are failing more than they succeed, the system stops trying. |
| **Confidence floor (0.80)** | Low-confidence AI diagnoses can never reach an action — they escalate. |
| **Namespace gates** | Production-like namespaces require an explicit human approval for autonomous action. |
| **Failure memory** | When a fix is verified to have made things worse, that pattern is stored so the AI prompt is poisoned against suggesting it again. |
| **Token encryption at rest** | Your GitHub token is Fernet-encrypted in Postgres; only decrypted in-process at the moment of the GitHub API call. |
| **Idempotent ingestion** | A flapping alert that fires the same fingerprint within 120 seconds is dropped. The dedup key includes container & severity. |

---

## 12. Tradeoffs You Should Know About

| Tradeoff | Choice we made | What it costs |
|---|---|---|
| **GitOps PR vs. live `kubectl apply`** | PR | Slower than auto-apply, but auditable, reviewable, and your existing CI runs against it. |
| **Two-tier diagnosis (rules first, AI second)** | Rules first | Most incidents are handled with no LLM cost or latency. The AI only runs on novel anomalies. |
| **Bounded action vocabulary** | Only ~5 actions | Cannot do creative fixes. We accept this for safety. |
| **Strict Pydantic schema on AI output** | No free text | The LLM cannot return `"hmm I think maybe..."`. Either it conforms or we reject and escalate. |
| **5-minute stability window before declaring success** | Slow but real | A naive `metric dropped` check would mark transient dips as success. We require sustained recovery. |
| **In-memory Mongo fallback** | Pipeline keeps running with telemetry context loss | Better than blocking the whole pipeline on Atlas TLS hiccups. |
| **Local Qdrant in-memory mode** | RAG memory is per-process | Restarts wipe the memory. Acceptable for a research/staging install; use a Qdrant server for multi-instance production. |
| **One project per install** | Simpler UI | Multi-tenant separation is enforced in the data model but not yet in the dashboard navigation. |

---

## 13. Troubleshooting

### "I posted a webhook but no incident shows up"
- Check `tail -f api.log` — if you see `[INGEST] dropped duplicate`, the
  120s dedup window already covered it. Change one label or wait.
- If you see no `[INGEST]` line at all, the request never reached the API.
  Verify `curl http://localhost:8010/api/v1/status` returns JSON.

### "An incident is stuck in `INGESTED`"
- The Celery worker is not running, or not connected to the same Redis.
- `docker exec autofixops-redis-1 redis-cli LLEN celery` should be 0
  when idle and >0 just after a webhook.

### "An incident is stuck in `PENDING_PR_MERGE`"
- That's normal — the system is waiting for you to merge the PR. Open
  the PR link, review, merge.
- Maximum wait is 30 polls × 60 seconds = 30 minutes by default.
  After that the incident is marked `FAILED` (`reason: pr_not_merged_in_time`).

### "AI diagnosis fails with `'QdrantClient' object has no attribute 'search'`"
- You're on an old commit. `git pull origin main` — this was fixed
  in commit `9b0e4c9`.

### "MongoDB SSL handshake fails"
- On macOS Python 3.9, the system OpenSSL is too old for Atlas. The
  system already falls back gracefully — you'll see
  `[DB INIT] MongoDB unreachable ... falling back to in-memory`.
  The pipeline still works; only persistent telemetry context is lost.
- To fix permanently, use a Python built against modern OpenSSL
  (`brew install python@3.12` or pyenv).

### "PR was created but is empty / wrong file changed"
- Check the **Resolved Target** card on the incident detail page — it
  shows what the resolver picked. If the namespace/deployment is wrong,
  fix the alert labels (Prometheus labels → resolver inputs).
- Check `TARGET_MANIFEST_PATH` matches an actual file in the repo.

### "Circuit breaker is OPEN and won't reset"
- It's a 30-minute cooldown by design. To force it closed, set system
  mode to `DISABLED`, then back to `ACTIVE` — that resets in-memory
  state on the worker.

### "Where do the logs live?"
- `api.log` — uvicorn / FastAPI
- `celery.log` — workers (this is where the `[TRACE:...]` lines flow)
- Dashboard logs print to the terminal where you ran `npm run dev`.

---

## 14. FAQ

**Q. Will AutoFixOps ever merge a PR for me?**
No. The merge button is yours.

**Q. Does AutoFixOps need write access to my Kubernetes cluster?**
No. It only reads metrics from Prometheus and writes to your GitHub
repo. Whatever applies the merged YAML (Argo CD, Flux, your CI) is
your existing system, untouched.

**Q. What if the alert was a false positive?**
Reject the PR. The incident is marked `PR_REJECTED` and that pattern
is recorded so the system gets less confident about similar future
incidents.

**Q. Can I plug in a different LLM?**
Yes. Set `OPENAI_API_BASE` to any OpenAI-compatible endpoint (Ollama,
vLLM, Anthropic-via-proxy, your private gateway). The model name
goes in `OPENAI_MODEL_NAME`. The system enforces a strict schema
regardless of the model.

**Q. How much does it cost to run?**
- **No LLM cost** for the common cases — the rule engine handles them.
- **AI fallback** is one prompt per novel incident, ~2k tokens in,
  ~200 tokens out.
- Infra cost: one small Postgres, one tiny Redis, optional MongoDB,
  optional Qdrant. The defaults all run on free tiers.

**Q. Is this safe to run on production?**
Run it in **shadow mode** for at least two weeks. Look at the metrics
page. If the rule-engine hit rate is high, the AI fallback rate is
low, and the policy approval rate matches your gut, switch to
`ACTIVE`. The dashboard's mode dropdown is the one switch you need.

**Q. How do I uninstall?**
Stop the three processes (API, worker, dashboard). The state in
Postgres / Mongo / Qdrant / GitHub is yours and nothing is hooked
into your cluster, so there is nothing else to clean up.

---

*Last updated: 2026-04-30. Repository: https://github.com/vanshitahujaa/Auto_fix_Ops*
