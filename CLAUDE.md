# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AutoFixOps is a self-healing loop for Kubernetes workloads: Prometheus/Alertmanager alerts → FastAPI ingestion → Celery pipeline (context → diagnosis → policy → GitHub PR → verification). Remediation is always a **Pull Request against a manifest in a GitHub repo**, never a live `kubectl apply`. `USER_GUIDE.md` is the most accurate description of current behavior; `TECHNICAL_DESIGN.md` is the original aspirational design (mentions Loki, OTel, LangChain RAG, Slack approvals — not all of that is implemented).

## Running locally

Config comes from `.env` (see `.env.example`; loaded via `python-dotenv` in `api/database.py`). Postgres (`POSTGRES_URL`) is required; MongoDB (`MONGO_URL`) is optional and falls back to in-memory; `OPENAI_API_KEY` is only needed for the AI fallback.

```bash
docker compose up -d                 # redis, postgres (host port 5433), prometheus (9090), alertmanager (9093)
python scripts/migrate_schema.py     # idempotent ALTER TABLE ... ADD COLUMN IF NOT EXISTS
uvicorn api.main:app --host 0.0.0.0 --port 8010
celery -A workers.celery_app worker --loglevel=info --pool=solo   # --pool=solo needed on Windows
cd dashboard && npm install && npm run dev                         # Next.js on :3000, API via NEXT_PUBLIC_API_URL
```

Chaos target app (the thing being monitored): `kubernetes_integration/target_app/app.py` — Prometheus in docker-compose scrapes it at `host.docker.internal:8001`, and Alertmanager posts to `host.docker.internal:8010/api/v1/alerts`. Alert rules live in `monitoring/alert_rules.yml`.

## Tests

There is no pytest config; tests are standalone scripts with `if __name__ == "__main__"` runners (they also work under pytest). Run from the repo root:

```bash
python tests/safety_drills/test_bad_patch.py        # or any file in tests/safety_drills/
python -m pytest tests/safety_drills/test_bad_patch.py::test_unknown_action_raises
python tests/evaluator/harness.py                   # diagnosis accuracy over tests/evaluator/dataset.json
```

No lint config exists for Python or the dashboard.

## Architecture

**Pipeline (`workers/tasks.py`)** — each Celery task does its step, transitions the incident, then enqueues the next via `apply_async`:
`build_incident_context` → `diagnose_incident` → `evaluate_policy` → `execute_remediation` → `verify_resolution` (delayed; polls PR merge, then checks a stability window in Prometheus). Every task first calls `_check_kill_switch` and checks the incident is in the expected status, skipping otherwise (makes retries/duplicates safe). `PIPELINE_STOP_AFTER_POLICY=true` halts after policy (demo mode).

**Incident state machine (`api/models.py`)** — `Incident.transition_to()` enforces `VALID_TRANSITIONS` and raises on illegal jumps; always use it rather than assigning `status`. It also appends to `status_timeline`. New columns on models need a matching entry in `scripts/migrate_schema.py` (tables are only `create_all`'d, never altered, at startup).

**Diagnosis is two-tier** — `engine/baseline.py` `RuleBasedDiagnosisEngine` (keyed on alert name) runs first; only `UNKNOWN_ANOMALY` falls through to `engine/ai_diagnostics.py` (OpenAI via LangChain, strict Pydantic output, confidence floor 0.80). The AI sees `engine/summarizer.py`'s compact summary, never raw metric arrays, plus RAG hits from `engine/memory.py` (Qdrant with sentence-transformers; separate collections for successful resolutions and known-bad fixes).

**Safety gates (`engine/policy.py`, `engine/circuit_breaker.py`)** — kill switch, action allowlist, confidence floor, protected namespaces, anti-thrashing (≥3 attempts/60 min), per-project/per-action circuit breaker. Any failure → `ESCALATED`, which a human can approve via `POST /api/v1/escalations/{id}/approve`. The action vocabulary is deliberately fixed: `INCREASE_MEMORY_LIMIT`, `INCREASE_CPU_LIMIT`, `RESTART_POD`, `ROLLBACK_DEPLOYMENT`, `DISABLE_FEATURE_FLAG` (allowlisted flags in `engine/patch_safety.py`, `"true"` → `"false"` only), `ESCALATE`.

**Remediation** — `engine/target_resolver.py` maps alert labels to a namespace/deployment/container; `engine/patch_generator.py` patches the manifest YAML (bounded: 2× cap, 4Gi/4 CPU ceiling; records previous values for exact rollback; must not mutate its input); `engine/remediation.py` commits and opens the PR via PyGithub (simulates if no token; `SHADOW_MODE` makes PRs drafts titled `[SHADOW]`). `engine/verification.py` decides success/failure and feeds the circuit breaker and failure memory.

**Config/credentials (`api/config_helpers.py`)** — GitHub creds resolve as: `ServiceAccount` row (Fernet-encrypted token in Postgres, "bot-only" auth) → `.env` `GITHUB_TOKEN`. Project config and system mode (`ACTIVE`/`SHADOW`/`DISABLED`) are read through TTL caches here; call the `invalidate_*` helpers after writes.

**AI path (Stages 2–4)** — with `AI_INCIDENT_ANALYSIS=true` diagnosis stops at `DIAGNOSED`; Stage 2 (`engine/patch_routing.py`, `engine/patch_proposer.py`) produces a sandbox-validated proposal (route by confidence 0.80; the LLM only picks a menu value). Stages 3–4 (`engine/remediation_workflow.py`, table `remediation_runs`, Celery task `advance_remediation`, API `api/remediation.py`) take a human-approved VALIDATED proposal through GitHub PR (`engine/github_gateway.py`), controlled kubectl deployment (`engine/deployer.py`, off unless `DEPLOY_MODE=kubectl`) and recovery verification (`engine/recovery_verifier.py`). Tests use fakes for GitHub/kubectl; see `docs/ARCHITECTURE.md` and `docs/RUNNING_ON_WINDOWS.md`.

**Real-time events (`api/events.py`)** — Celery workers call `emit_sync(...)`, which publishes to Redis PubSub channel `autofixops_events`; the API bridges that to the `/api/v1/events/ws` WebSocket consumed by the dashboard.

**Chaos** — `POST /api/v1/chaos/inject` hits the target app to trigger faults and creates an incident directly (doesn't wait for Alertmanager).

## Dashboard

`dashboard/` is Next.js 16 / React 19 (App Router, plain JS, Recharts). Per `dashboard/AGENTS.md`: this Next.js version has breaking changes from what you may know — read the relevant guide in `dashboard/node_modules/next/dist/docs/` before writing Next.js code. All backend calls go through `dashboard/lib/api.js`.

## Debugging

Logs are tagged `[TRACE:<incident_id>]` across API and worker, so grep one incident's full path through the pipeline. An incident stuck in `INGESTED` usually means the Celery worker isn't running or is on a different Redis.
