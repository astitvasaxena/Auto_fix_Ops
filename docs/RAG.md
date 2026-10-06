# RAG Knowledge Layer

AutoFixOps retrieves **operational guidance curated from official Kubernetes and
Prometheus documentation** and gives it to the AI Incident Analyst together with
the current incident's evidence. The documents are embedded with the local
`all-MiniLM-L6-v2` SentenceTransformer (384-dim, cosine) and stored in Qdrant.
Code: `engine/runbooks.py` (the documents), `engine/memory.py` (the store),
`engine/ai_diagnostics.py` (retrieval for the analyst).

## What RAG is (and is not)

- The documents are **guidance**: what to check, how to interpret evidence, and
  what to weigh before changing anything.
- They are **not historical incidents**, not records of past AutoFixOps runs, and
  they **do not prove anything** about the current incident.
- They are **not automatic remediation rules**. Nothing in RAG authorizes an action.
  The **Policy Engine** stays responsible for action authorization, and the
  analysis stage stops at `DIAGNOSED`.
- The AI combines **current incident evidence** with retrieved guidance. Its prompt
  says to claim only evidence present in the incident context, and never to invent
  Kubernetes events, OOMKilled status, exit codes, CPU/memory values, logs, Pod names,
  remediation outcomes or source URLs. Source metadata on each citation is attached
  from the store, not taken from the model's output.

## Knowledge base (6 documents, `operational_runbooks` collection)

| runbook_id | Topic | Primary official source |
|---|---|---|
| `k8s-repeated-container-failure` | Repeated container failure / CrashLoopBackOff | Kubernetes — Debug Running Pods |
| `k8s-oomkilled-memory-pressure` | Memory usage, limits, OOMKilled | Kubernetes — Assign Memory Resources to Containers and Pods |
| `k8s-cpu-limits-throttling` | High CPU, CPU limits, throttling | Kubernetes — Resource Management for Pods and Containers |
| `k8s-application-troubleshooting` | General application troubleshooting workflow | Kubernetes — Troubleshooting Applications |
| `prometheus-target-availability` | `up == 0` / target down | Prometheus — Alerting rules |
| `prometheus-alert-triage` | Using alerts as investigation evidence | Prometheus — Alerting (best practices) |

Each document is paraphrased, not copied, and follows the same structure:
**symptom → evidence to collect → possible interpretations → diagnostic steps →
remediation considerations → safety limitations**.

### Official sources

1. Kubernetes — Monitoring, Logging, and Debugging: https://kubernetes.io/docs/tasks/debug/
2. Kubernetes — Troubleshooting Applications: https://kubernetes.io/docs/tasks/debug/debug-application/
3. Kubernetes — Debug Running Pods: https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/
4. Kubernetes — Assign Memory Resources to Containers and Pods: https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/
5. Kubernetes — Resource Management for Pods and Containers: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
6. Prometheus — Alerting rules: https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/
7. Prometheus — Alerting (best practices): https://prometheus.io/docs/practices/alerting/
8. Prometheus — Alerting overview: https://prometheus.io/docs/alerting/latest/overview/

No blogs, forums or generated content are used as sources.

## Document metadata

| Field | Meaning |
|---|---|
| `source_type` | `official_documentation`: guidance derived from official vendor docs. `upsert_runbooks()` refuses anything else. |
| `source_status` | `curated`: paraphrased and reviewed by a human; not learned from incidents. |
| `verified` | `true`: content checked against the cited official page (`reviewed_on`). It does **not** mean the guidance was validated by an AutoFixOps run. |
| `source_organization`, `source_title`, `source_url` | Primary official source. |
| `additional_sources` | Other official pages the document draws on. |
| `topic`, `incident_types`, `incident_type` | Coverage; `incident_type` is the primary (first) type. |
| `guidance_type` | `operational_guidance`. |
| `disclaimer` | "Curated from official documentation; not a historical incident and not an automatic remediation rule." |
| `sections`, `content` | Structured sections, and the full rendered text shown to the AI. |

Only a short profile (title, topic, symptom, retrieval terms, alert names) is
embedded, because the model truncates long inputs. Queries and documents both go
through `to_embedding_text()`.

## Retrieval in the analysis stage

`diagnose_incident` builds a query from the alert name, annotations, chaos fault
type and the rule-engine hint. It then calls `retrieve_runbooks()` and keeps the top
`RAG_RUNBOOK_TOP_K` documents (default 3) that score at least `RAG_RUNBOOK_MIN_SCORE`
(default 0.35). Retrieved documents reach the LLM with their source organization,
title, URL, labels and similarity score. If none qualify, the prompt says explicitly
that no relevant guidance was found. The `incident_resolutions` / `incident_failures`
collections are not queried in this stage.

## Why the existing incidents were not seeded

The 66 incidents in PostgreSQL (inspected 2026-10-06) contain one repeated pattern
whose "resolved" cases all came from simulated PRs. No fix was ever applied, and
there are no failed-fix records or telemetry. Seeding them would teach the RAG
outcomes that never happened.

## Storage configuration

| Variable | Effect |
|---|---|
| *(neither set)* | In-memory; lost when the process exits. |
| `QDRANT_PATH=./qdrant_data` | Local on-disk store. Only **one process** can open it at a time. |
| `QDRANT_URL=http://localhost:6333` | Qdrant server; safe for several processes. `QDRANT_API_KEY` is optional. |

## Seeding

```bash
# QDRANT_PATH or QDRANT_URL must be set; with QDRANT_PATH, stop the Celery worker first
python scripts/seed_rag_runbooks.py
python scripts/seed_rag_runbooks.py --query "target application is crashing repeatedly"
```

The seed makes the collection hold **exactly** the current documents. It upserts
them under deterministic ids and deletes every other point, which retires the old
synthetic runbooks. Re-running it changes nothing. It never runs automatically.

## Testing

```bash
python tests/rag/test_runbooks.py
```

The tests run offline (cached embedding model, in-memory or temporary Qdrant; URLs
are checked, never fetched). They cover the document count, labels and official
URLs, the document structure, the absence of synthetic documents, idempotent
seeding and replacement of the legacy runbooks, retrieval for crash, memory, CPU
and target-down queries, source metadata and scores, and the seed script end to end.
