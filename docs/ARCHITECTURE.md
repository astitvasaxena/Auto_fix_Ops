# AutoFixOps — Architecture (as implemented)

This document describes the code as it is today. `TECHNICAL_DESIGN.md` is the
original design and mentions parts (Loki, OpenTelemetry, Slack approvals) that
were never built.

## 1. End-to-end flow

```
 Target App ──/metrics──▶ Prometheus ──alert rule──▶ Alertmanager ──webhook──▶ AutoFixOps API
 (FastAPI, :8001 local                (monitoring/          (POST /api/v1/alerts)       │
  or K8s pod via NodePort :30081)      alert_rules.yml)                                  ▼
                                                                         Incident INGESTED (Postgres)
                                                                                         │ Celery
 ┌───────────────────────────────────────────────────────────────────────────────────────┘
 ▼
 Task 1  Context      engine/telemetry.py: memory/CPU peak from Prometheus      → CONTEXT_BUILT
 Task 2  Diagnosis    rule engine + runbook RAG (Qdrant) + local LLM report     → DIAGNOSED
 Stage 2 Proposal     confidence routing (0.80) → proposer: LLM picks a value
                      from a code-made menu, Python builds + sandbox-validates
                      the diff → PatchProposal VALIDATED / NOT_GENERATED / ...
 Stage 3 Approval     human approves the exact validated proposal              → POLICY_APPROVED
         GitHub PR    branch + commit + PR (engine/github_gateway.py)           → REMEDIATING → PENDING_PR_MERGE
         Merge        a human merges; the merged file must equal the patch
 Stage 4 Deployment   kubectl JSON patch of one limit (engine/deployer.py)
         Verification health, pods, alert, headroom stable for N seconds        → VERIFIED → RESOLVED
                      (failure → exact inverse patch + revert PR)               → FAILED
```

The dashboard (Next.js, `dashboard/`) reads everything from the API and shows a
stage as completed only when the backend has recorded it.

## 2. Components

| Area | Module | Responsibility |
|---|---|---|
| Ingestion | `api/main.py` | Alertmanager webhook, de-duplication, incident CRUD, chaos endpoint, kill switch |
| Pipeline | `workers/tasks.py` | Celery tasks: context → diagnosis → (Stage 2 hook) and `advance_remediation` |
| Telemetry | `engine/telemetry.py` | cAdvisor container metrics first, then the target's own `process_*` metrics; peak over 5 min; bytes / cores |
| Diagnosis | `engine/baseline.py`, `engine/ai_diagnostics.py`, `engine/runbooks.py`, `engine/memory.py` | Deterministic rules, curated official runbooks in Qdrant, LLM analyst report (strict schema) |
| Stage 2 | `engine/patch_routing.py`, `engine/patch_proposer.py`, `engine/patch_diff.py`, `engine/patch_safety.py`, `api/patch_proposals.py` | Confidence routing, human approval of generation, bounded proposal, 16 mandatory validation checks |
| Stage 3–4 | `engine/remediation_workflow.py`, `engine/github_gateway.py`, `engine/deployer.py`, `engine/recovery_verifier.py`, `api/remediation.py` | Approval, PR, controlled deployment, verification, rollback, timeline |
| Dashboard | `dashboard/lib/pipeline.mjs`, `components/PipelineFlow.js`, `components/AIRemediationPanel.js`, `components/RemediationPanel.js` | Backend-confirmed pipeline view, approval controls |

## 3. State machines

**Incident** (`api/models.py`, `Incident.transition_to` enforces it):

```
INGESTED → CONTEXT_BUILT → DIAGNOSED → POLICY_APPROVED → REMEDIATING → PENDING_PR_MERGE → VERIFIED → RESOLVED
                               │                              └──────────(PR step disabled)──┘
                               ├→ ESCALATED → RESOLVED (manual close) / POLICY_APPROVED (legacy path)
 any non-final state → FAILED
```

**Remediation run** (`remediation_runs`, one per validated proposal):

```
APPROVED → PR_OPEN → READY_TO_DEPLOY → DEPLOYING → VERIFYING → RESOLVED
    └──────(REMEDIATION_REQUIRE_PR=false)──┘                   ├→ ROLLED_BACK
REJECTED (final)   FAILED (final: PR closed, merged content differs, drift, ...)   └→ ROLLBACK_FAILED
```

A step that cannot run yet (GitHub or deployment not configured, PR not merged,
target busy, cluster unreachable, shadow mode, kill switch) is recorded as
`blocked_code` and **changes nothing**; it is retried by the next scheduled
check or by the dashboard's *Retry this step* button.

## 4. Safety boundaries

| Boundary | Where | Rule |
|---|---|---|
| Kill switch | every task, approvals | `DISABLED` refuses approvals and blocks every remediation step |
| AI output | `patch_proposer.py` | The LLM only chooses one value from a code-generated menu (or declines); it never writes YAML, paths, commands or diffs. Its rationale may only use numbers from the facts. |
| Proposal validity | 16 mandatory checks | path allowlist and containment, manifest shape, bounds (≤2×, ≤4Gi / 4 CPU), strict diff policy, sandbox apply, PatchGenerator cross-check, line endings, real tree untouched |
| Confidence routing | `routing_policy.py` | One threshold 0.80 computed from the stored report; below it generation needs a recorded human approval |
| Remediation approval | `remediation_workflow.decide` | Always human; only the latest, integrity-checked VALIDATED proposal built from the current diagnosis |
| Approver identity | `api/remediation.py` | `APPROVER_TOKENS` → authenticated identity; otherwise loopback-only and recorded as unauthenticated (no invented identity) |
| Git integrity | `_patched_content`, `_check_pr` | PR content rebuilt from the stored diff and hashed; a merged file that differs from the approved patch is never deployed |
| Deployment scope | `deployer.py` | Off by default; explicit kubectl context; target allowlist; protected namespaces refused; one RFC 6902 patch with `test` ops (drift/race ⇒ nothing changes); server dry run first; no shell |
| Concurrency | `remediation_runs` | claim token per step; partial unique index ⇒ one deploying run per target |
| Recovery | `recovery_verifier.py` | all checks green for the whole stabilization window, otherwise exact inverse patch and revert PR |

## 5. Data model (new in Stages 2–4)

* `patch_proposals` — one row per proposer run (diff, hashes, evidence, validation steps).
* `patch_routing_decisions` — route and generation approval per diagnosis version.
* `remediation_runs` — approval (who/why/when), GitHub (branch, PR, merge, hashes),
  deployment (previous container spec, JSON patch, kubectl results), verification
  rounds, rollback, append-only `audit_log`.

## 6. Local vs Kubernetes target

Prometheus reads the target address from `monitoring/targets/target_app.json`.
`python scripts/k8s_demo.py target local|k8s` switches between the local process
(port 8001, Windows, no real memory limit) and the demo pod (NodePort 30081,
real 100Mi limit). Stage 4 deployment and verification need the Kubernetes target.
