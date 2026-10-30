"""
AutoFixOps Pipeline Orchestration
==================================
Each Celery task represents one discrete stage of the incident lifecycle.
Each task reads the current state, does its work, commits the new state,
then dispatches the next task. If any task fails, it sets FAILED and
stops the chain.

All tasks check SYSTEM_MODE at the top — DISABLED = immediate return.
All tasks thread project_id through the pipeline.

Pipeline:
  build_incident_context  →  CONTEXT_BUILT
  diagnose_incident       →  DIAGNOSED
                             (AI_INCIDENT_ANALYSIS=true, the default: stores the
                              AI Incident Report and STOPS here — nothing below runs)
  evaluate_policy         →  POLICY_APPROVED / ESCALATED
  execute_remediation     →  PENDING_PR_MERGE
  verify_resolution       →  RESOLVED / FAILED

Set PIPELINE_STOP_AFTER_POLICY=true only for a presentation that ends at the
policy decision. It preserves the normal remediation pipeline by default.
"""

import os
import requests
import time
import datetime
import uuid
from .celery_app import celery_app
from api.database import SessionLocal, incident_contexts_collection, logger
from api.models import Incident, IncidentStatus
from api.config_helpers import get_project_config, get_system_mode, is_system_disabled
from api.events import emit_sync

# Engine imports
from engine.summarizer import ContextSummarizer
from engine.baseline import RuleBasedDiagnosisEngine
from engine.ai_diagnostics import (
    AIDiagnosisEngine, build_analysis_context, build_runbook_query, retrieve_runbook_evidence,
)
from engine.memory import QdrantMemoryStore
from engine.policy import PolicyDecisionEngine
from engine.remediation import RemediationEngine
from engine.verification import VerificationEngine
from engine.target_resolver import TargetResolver, TargetResolutionError
from engine.telemetry import collect_telemetry
from engine import debug_trace as _dt


def _check_kill_switch(incident_id: str) -> bool:
    """Returns True if system is disabled (task should abort)."""
    if is_system_disabled():
        logger.warning(
            f"[TRACE:{incident_id}] [KILL SWITCH] System is DISABLED. "
            f"Skipping task execution."
        )
        return True
    return False


_DEFAULT_PROM_URL = os.getenv("PROMETHEUS_URL", "").strip()


def _stop_after_policy() -> bool:
    """Returns whether this run is intentionally limited to policy evaluation."""
    return os.getenv("PIPELINE_STOP_AFTER_POLICY", "false").strip().lower() == "true"


def _ai_analysis_enabled() -> bool:
    """AI Incident Analysis stage: diagnose with context + runbook RAG + LLM report,
    then stop at DIAGNOSED. Set AI_INCIDENT_ANALYSIS=false for the legacy
    diagnosis → policy → remediation pipeline."""
    return os.getenv("AI_INCIDENT_ANALYSIS", "true").strip().lower() == "true"


def _generate_ai_report(incident, context_doc: dict, summary, rule_verdict: dict,
                        debug_trace: dict = None) -> dict:
    """Builds context, retrieves curated runbooks and asks the analyst LLM for a report.
    Raises on any LLM failure so the caller's retry handling can mark FAILED."""
    incident_id = str(incident.id)
    # Build the engine first: if the LLM is unavailable (e.g. no API key) fail fast.
    ai_engine = AIDiagnosisEngine()

    context = build_analysis_context(
        incident_id, incident.alert_name, incident.severity,
        incident.raw_payload_cache, context_doc, summary,
    )

    # ── Debug: record structured context ──
    if debug_trace is not None:
        _dt.record_context(debug_trace, context)

    min_score = float(os.getenv("RAG_RUNBOOK_MIN_SCORE", "0.35"))
    top_k = int(os.getenv("RAG_RUNBOOK_TOP_K", "3"))
    query = build_runbook_query(context, rule_verdict)
    hits, rag_status = retrieve_runbook_evidence(QdrantMemoryStore(), query, top_k, min_score)
    logger.info(
        f"[TRACE:{incident_id}] [AI ANALYST] Runbook RAG status={rag_status} "
        f"hits={[(h['title'], h['similarity_score']) for h in hits]}"
    )

    # ── Debug: record RAG retrieval ──
    if debug_trace is not None:
        _dt.record_rag(
            debug_trace, "ai_analysis",
            query=query, collection="operational_runbooks",
            hits=hits, status=rag_status,
            min_score=min_score, top_k=top_k,
        )

    rag_meta = {
        "collection": "operational_runbooks", "query": query,
        "min_score": min_score, "top_k": top_k, "status": rag_status,
    }
    return ai_engine.analyze_incident(context, rule_verdict, hits, rag_meta,
                                      debug_trace=debug_trace)


def _get_prometheus_url(project_id: str = None) -> str:
    """Gets Prometheus URL from project config, falling back to env, then to in-cluster DNS."""
    if project_id:
        config = get_project_config(project_id)
        if config and config.get("prometheus_url"):
            return config["prometheus_url"]
    if _DEFAULT_PROM_URL:
        return _DEFAULT_PROM_URL
    return "http://prometheus-operated.autofixops.svc.cluster.local:9090"


def fetch_prometheus_metric(query: str, prometheus_url: str = None):
    """Executes a promQL query with strict timeouts. Returns [] on any failure."""
    url = prometheus_url or _get_prometheus_url()
    try:
        response = requests.get(
            f"{url}/api/v1/query",
            params={"query": query},
            timeout=3,
        )
        response.raise_for_status()
        return response.json().get("data", {}).get("result", [])
    except requests.Timeout:
        logger.warning(f"Prometheus query timed out (3s) — degrading to empty context.")
        return []
    except requests.exceptions.ConnectionError:
        logger.warning(f"Prometheus unreachable at {url} — degrading to empty context.")
        return []
    except Exception as e:
        logger.warning(f"Prometheus query failed ({e.__class__.__name__}) — degrading to empty context.")
        return []


# ════════════════════════════════════════════════════════════
# TASK 1: Context Gathering
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=3, time_limit=120, soft_time_limit=90)
def build_incident_context(self, incident_id: str):
    """Gathers telemetry from Prometheus, stores in MongoDB, transitions to CONTEXT_BUILT."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 1: CONTEXT] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            logger.error(f"[TRACE:{incident_id}] Incident not found.")
            return False

        # Idempotency guard
        if incident.status != IncidentStatus.INGESTED:
            logger.warning(f"[TRACE:{incident_id}] Already past INGESTED. Skipping.")
            return True

        project_id = str(incident.project_id) if incident.project_id else None
        prometheus_url = _get_prometheus_url(project_id)
        pod_name = incident.raw_payload_cache.get("labels", {}).get("pod", "unknown")

        # Gather evidence: Kubernetes container metrics first, then the target
        # app's own process metrics (local demo). Memory in bytes, CPU in cores.
        telemetry = collect_telemetry(
            lambda q: fetch_prometheus_metric(q, prometheus_url),
            incident.raw_payload_cache.get("labels", {}),
        )
        cpu_data = telemetry["metrics"]["cpu"]
        mem_data = telemetry["metrics"]["memory"]
        logger.info(
            f"[TRACE:{incident_id}] Telemetry source={telemetry['source']} "
            f"memory_series={len(mem_data)} cpu_series={len(cpu_data)}"
        )

        raw_payload = incident.raw_payload_cache or {}
        raw_context = {
            "incident_id": str(incident_id),
            "project_id": project_id or "unavailable",
            "stage": "PRE_DIAGNOSIS",
            "status": "CONTEXT_BUILT",
            "collected_at": datetime.datetime.utcnow().isoformat() + "Z",
            "timestamp": time.time(),
            "alert": {
                "name": incident.alert_name,
                "severity": incident.severity,
                "labels": raw_payload.get("labels", {}),
                "annotations": raw_payload.get("annotations", {}),
            },
            "target": {
                "pod": pod_name if pod_name != "unknown" else "unavailable",
                "namespace": raw_payload.get("labels", {}).get("namespace", "autofixops"),
                "project_id": project_id or "unavailable",
            },
            "chaos": {
                "is_chaos": bool(raw_payload.get("chaos", False)),
                "fault_type": raw_payload.get("fault_type", "unavailable"),
                "target_url": raw_payload.get("target_url", "unavailable"),
            },
            "telemetry": {
                "cpu_metrics": cpu_data if cpu_data else "unavailable",
                "memory_metrics": mem_data if mem_data else "unavailable",
                "cpu_available": bool(cpu_data),
                "memory_available": bool(mem_data),
                "prometheus_url": prometheus_url or "unavailable",
                "source": telemetry["source"] or "unavailable",
                "units": telemetry["units"],
                "queries": telemetry["queries"],
            },
            # Compatibility with existing ContextSummarizer & MongoDB consumers
            "metrics": {"cpu": cpu_data, "memory": mem_data},
            "target_pod": pod_name,
        }

        # 1. Persist to PostgreSQL in incident.raw_payload_cache
        payload = dict(raw_payload)
        payload["raw_context"] = raw_context
        incident.raw_payload_cache = payload

        # 2. Persist to disk trace immediately (before diagnose_incident runs)
        _dt.save_pre_diagnosis_trace(str(incident_id), raw_context)

        # 3. Persist to MongoDB (graceful degradation)
        try:
            incident_contexts_collection.update_one(
                {"incident_id": incident_id}, {"$set": raw_context}, upsert=True
            )
        except Exception as mongo_err:
            logger.warning(
                f"[TRACE:{incident_id}] MongoDB write failed ({mongo_err.__class__.__name__}); "
                f"continuing without persistent context."
            )

        # State transition
        incident.transition_to(IncidentStatus.CONTEXT_BUILT)
        emit_sync("incident.status_changed", {"new_status": "CONTEXT_BUILT"}, str(incident.id))
        db.commit()

        logger.info(f"[TRACE:{incident_id}] === [TASK 1: CONTEXT] DONE → CONTEXT_BUILT ===")

        # Chain to next task
        diagnose_incident.apply_async(args=[incident_id])
        return True

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 1 FAILED] {e}")
        return _retry_or_fail(self, db, incident_id, e, "build_incident_context")
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# TASK 2: Diagnosis (Rule Engine → AI Fallback)
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=2)
def diagnose_incident(self, incident_id: str):
    """Runs Rule Engine first. Falls through to AI on UNKNOWN. Stores verdict on Incident."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 2: DIAGNOSIS] START ===")
    db = SessionLocal()

    # ── Debug: create or load per-incident trace envelope ──
    trace = _dt.load_or_new_trace(str(incident_id))

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            return False

        if incident.status != IncidentStatus.CONTEXT_BUILT:
            logger.warning(f"[TRACE:{incident_id}] Not in CONTEXT_BUILT state. Skipping.")
            return True

        # Fetch context from Mongo (degrade gracefully if Atlas is down)
        try:
            context_doc = incident_contexts_collection.find_one({"incident_id": incident_id})
        except Exception as mongo_err:
            logger.warning(
                f"[TRACE:{incident_id}] MongoDB read failed ({mongo_err.__class__.__name__}); "
                f"checking relational DB context."
            )
            context_doc = None

        if not context_doc and incident.raw_payload_cache and "raw_context" in incident.raw_payload_cache:
            context_doc = incident.raw_payload_cache["raw_context"]

        if not context_doc:
            logger.warning(
                f"[TRACE:{incident_id}] No context document available — "
                f"diagnosing on alert metadata only."
            )
            context_doc = {"incident_id": incident_id, "metrics": {"cpu": [], "memory": []}}

        alert_name = incident.alert_name

        # Summarize
        _dt.record_stage_start(trace, "summarize")
        summary = ContextSummarizer.summarize(context_doc)
        _dt.record_stage_end(trace, "summarize", summary=summary.model_dump())
        logger.info(f"[TRACE:{incident_id}] Summary: {summary.model_dump_json()}")

        # Rule Engine first
        _dt.record_stage_start(trace, "rule_engine")
        rule_engine = RuleBasedDiagnosisEngine()
        verdict = rule_engine.analyze(alert_name, context_doc)
        _dt.record_rule_engine(trace, verdict)
        _dt.record_stage_end(trace, "rule_engine")

        # Analysis stage: context + runbook RAG + LLM report, then STOP at DIAGNOSED.
        if _ai_analysis_enabled():
            _dt.record_stage_start(trace, "ai_analysis")
            report = _generate_ai_report(incident, context_doc, summary, verdict,
                                         debug_trace=trace)
            _dt.record_stage_end(trace, "ai_analysis")

            incident.ai_report = report
            incident.diagnosis_classification = report["incident_type"]
            incident.diagnosis_confidence = report["confidence"]
            incident.diagnosis_reasoning = report["root_cause_analysis"]
            incident.diagnosed_by = "AI_ENGINE"
            emit_sync("incident.diagnosed", {"classification": incident.diagnosis_classification, "confidence": incident.diagnosis_confidence, "engine": incident.diagnosed_by}, str(incident.id))

            incident.transition_to(IncidentStatus.DIAGNOSED)
            emit_sync("incident.status_changed", {"new_status": "DIAGNOSED"}, str(incident.id))
            emit_sync("incident.ai_report_ready", {"proposed_action": report["proposed_action"]["type"], "confidence": report["confidence"]}, str(incident.id))
            db.commit()

            # ── Debug: save trace to disk ──
            _dt.save_trace(trace)

            logger.info(
                f"[TRACE:{incident_id}] === [TASK 2: AI ANALYSIS] DONE → DIAGNOSED "
                f"(report ready; proposed {report['proposed_action']['type']}, NOT executed). "
                f"Stopping: policy/remediation are not dispatched while AI_INCIDENT_ANALYSIS=true ==="
            )
            _route_patch_proposal(incident_id)
            return True

        diagnosed_by = "RULE_ENGINE"

        # AI fallback if unknown
        if verdict.get("root_cause_classification") == "UNKNOWN_ANOMALY":
            logger.info(f"[TRACE:{incident_id}] Rule Engine → UNKNOWN. Invoking AI...")
            diagnosed_by = "AI_ENGINE"

            _dt.record_stage_start(trace, "legacy_rag")
            memory_store = QdrantMemoryStore()
            rag_hits = memory_store.retrieve_similar(summary.model_dump_json(), top_k=2)
            failed_hits = memory_store.retrieve_failures(summary.model_dump_json(), top_k=2)
            _dt.record_rag(
                trace, "legacy_rag",
                query=summary.model_dump_json()[:200],
                collection="incident_resolutions+incident_failures",
                hits=rag_hits + (failed_hits or []),
                status="ok" if rag_hits else "no_match",
                min_score=0.75, top_k=2,
            )
            _dt.record_stage_end(trace, "legacy_rag")

            _dt.record_stage_start(trace, "legacy_diagnosis")
            ai_engine = AIDiagnosisEngine()
            ai_verdict = ai_engine.analyze(summary, rag_hits, failed_hits=failed_hits,
                                           debug_trace=trace)
            _dt.record_stage_end(trace, "legacy_diagnosis")

            verdict = {
                "root_cause_classification": ai_verdict.get("diagnosis"),
                "confidence": ai_verdict.get("confidence", 0.0),
                "reasoning": " ".join(
                    ai_verdict.get("recommended_action", {}).get("reasoning", [])
                ),
                "action": ai_verdict.get("recommended_action"),
            }

        # Persist diagnosis on the incident record
        incident.diagnosis_classification = verdict.get("root_cause_classification")
        incident.diagnosis_confidence = verdict.get("confidence", 1.0)
        incident.diagnosis_reasoning = verdict.get("reasoning", "")
        incident.diagnosed_by = diagnosed_by
        emit_sync("incident.diagnosed", {"classification": incident.diagnosis_classification, "confidence": incident.diagnosis_confidence, "engine": incident.diagnosed_by}, str(incident.id))

        incident.transition_to(IncidentStatus.DIAGNOSED)
        emit_sync("incident.status_changed", {"new_status": "DIAGNOSED"}, str(incident.id))
        db.commit()

        # ── Debug: save trace to disk ──
        _dt.save_trace(trace)

        logger.info(
            f"[TRACE:{incident_id}] === [TASK 2: DIAGNOSIS] DONE → DIAGNOSED "
            f"({diagnosed_by}: {verdict.get('root_cause_classification')}) ==="
        )

        # Chain: pass verdict along
        evaluate_policy.apply_async(args=[incident_id, verdict])
        return True

    except Exception as e:
        # ── Debug: save trace even on failure ──
        _dt.record_stage_end(trace, "diagnosis_error", error=f"{e.__class__.__name__}: {e}")
        _dt.save_trace(trace)
        logger.error(f"[TRACE:{incident_id}] [TASK 2 FAILED] {e}")
        return _retry_or_fail(self, db, incident_id, e, "diagnose_incident")
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# TASK 3: Policy Evaluation
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=1)
def evaluate_policy(self, incident_id: str, verdict: dict):
    """Evaluates the diagnosis against policy gates. Routes to remediation or escalation."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 3: POLICY] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            return False

        if incident.status != IncidentStatus.DIAGNOSED:
            logger.warning(f"[TRACE:{incident_id}] Not in DIAGNOSED state. Skipping.")
            return True

        namespace = incident.raw_payload_cache.get("labels", {}).get(
            "namespace", "autofixops"
        )

        policy_engine = PolicyDecisionEngine()
        policy_result = policy_engine.evaluate(incident, verdict, namespace)

        # Policy evaluation is not a remediation action. Store its result with
        # the incident so the dashboard can show it without affecting the
        # anti-thrash history in remediation_audits.
        payload = dict(incident.raw_payload_cache or {})
        payload["policy_decision"] = {
            "decision": policy_result.decision,
            "reason": policy_result.reason,
            "action_type": verdict.get("action", {}).get("type", "UNKNOWN"),
            "evaluated_at": time.time(),
        }
        incident.raw_payload_cache = payload

        logger.info(
            f"[TRACE:{incident_id}] [POLICY RESULT] "
            f"{policy_result.decision}: {policy_result.reason}"
        )

        if policy_result.decision == "APPROVED":
            incident.transition_to(IncidentStatus.POLICY_APPROVED)
            emit_sync("incident.status_changed", {"new_status": "POLICY_APPROVED"}, str(incident.id))
            db.commit()

            if _stop_after_policy():
                logger.info(
                    f"[TRACE:{incident_id}] [PRESENTATION MODE] "
                    "Stopping after approved policy decision."
                )
            else:
                # Chain to remediation in the normal full pipeline.
                execute_remediation.apply_async(args=[incident_id, verdict])

        elif policy_result.decision in ("ESCALATED", "REJECTED"):
            incident.transition_to(IncidentStatus.ESCALATED)
            emit_sync("incident.status_changed", {"new_status": "ESCALATED"}, str(incident.id))
            db.commit()
            logger.info(
                f"[TRACE:{incident_id}] === [TASK 3: POLICY] DONE → ESCALATED ==="
            )
            # Pipeline stops here — human must intervene

        logger.info(f"[TRACE:{incident_id}] === [TASK 3: POLICY] DONE ===")
        return True

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 3 FAILED] {e}")
        _mark_failed(db, incident_id, reason=f"{e.__class__.__name__}: {e}", stage="evaluate_policy")
        return False
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# TASK 4: Remediation (GitOps PR) — with target resolution
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=2)
def execute_remediation(self, incident_id: str, verdict: dict):
    """Resolves target, validates, then creates the GitOps PR."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 4: REMEDIATION] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            return False

        if incident.status != IncidentStatus.POLICY_APPROVED:
            logger.warning(f"[TRACE:{incident_id}] Not in POLICY_APPROVED state. Skipping.")
            return True

        project_id = str(incident.project_id) if incident.project_id else None
        project_config = get_project_config(project_id) if project_id else {}

        # ─── Circuit breaker check ───
        from engine.circuit_breaker import get_circuit_breaker_registry
        action_type = verdict.get("action", {}).get("type", "UNKNOWN")

        if not get_circuit_breaker_registry().should_allow(
            project_id or "global", action_type
        ):
            logger.warning(
                f"[TRACE:{incident_id}] [CIRCUIT BREAKER] Blocked action {action_type}. "
                f"Escalating instead."
            )
            incident.transition_to(IncidentStatus.ESCALATED)
            emit_sync("incident.status_changed", {"new_status": "ESCALATED"}, str(incident.id))
            db.commit()
            return True

        # ─── Target resolution ───
        resolver = TargetResolver()
        try:
            context_doc = incident_contexts_collection.find_one({"incident_id": incident_id})
        except Exception as mongo_err:
            logger.warning(
                f"[TRACE:{incident_id}] MongoDB read failed in remediation "
                f"({mongo_err.__class__.__name__}); resolving without context."
            )
            context_doc = None

        try:
            resolved_target = resolver.resolve(
                incident.raw_payload_cache or {},
                project_config or {},
                context_doc,
            )
        except TargetResolutionError as e:
            logger.error(f"[TRACE:{incident_id}] [TARGET RESOLUTION FAILED] {e}")
            _mark_failed(db, incident_id, reason=f"TargetResolutionError: {e}", stage="execute_remediation")
            return False

        # Pre-execution validation
        if not resolver.validate_target(resolved_target):
            logger.error(f"[TRACE:{incident_id}] [TARGET VALIDATION FAILED]")
            _mark_failed(db, incident_id, reason="Target validation failed", stage="execute_remediation")
            return False

        # Store resolved target on incident
        incident.resolved_target = resolved_target
        incident.transition_to(IncidentStatus.REMEDIATING)
        emit_sync("incident.status_changed", {"new_status": "REMEDIATING"}, str(incident.id))
        db.commit()

        # Build summary text for the PR body
        summary_text = (
            f"CPU Peak: {incident.diagnosis_confidence}, "
            f"Classification: {incident.diagnosis_classification}, "
            f"Reasoning: {incident.diagnosis_reasoning}"
        )

        remediation_engine = RemediationEngine(project_config=project_config)
        result = remediation_engine.execute(incident_id, verdict, summary_text)

        incident.transition_to(IncidentStatus.PENDING_PR_MERGE)
        emit_sync("incident.status_changed", {"new_status": "PENDING_PR_MERGE"}, str(incident.id))
        db.commit()

        logger.info(
            f"[TRACE:{incident_id}] === [TASK 4: REMEDIATION] DONE → PENDING_PR_MERGE "
            f"(PR: {result.get('pr_url')}) ==="
        )

        # Capture baseline metrics for negative signal detection
        baseline_metrics = resolved_target.get("current_values", {})

        # Chain to verification
        pod_name = incident.raw_payload_cache.get("labels", {}).get("pod", "unknown")
        verify_resolution.apply_async(
            args=[
                incident_id,
                result.get("audit_id"),
                summary_text,
                incident.diagnosis_classification,
                action_type,
                pod_name,
                baseline_metrics,
            ],
            countdown=5,
        )
        return True

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 4 FAILED] {e}")
        return _retry_or_fail(self, db, incident_id, e, "execute_remediation")
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# TASK 5: Verification & Learning
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=1)
def verify_resolution(
    self,
    incident_id: str,
    audit_id: str,
    summary_text: str,
    diagnosis: str,
    action_taken: str,
    pod_name: str,
    baseline_metrics: dict = None,
):
    """Polls PR merge, stability window check, stores learning."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 5: VERIFICATION] START ===")

    try:
        # Get project config for this incident
        db = SessionLocal()
        try:
            incident = db.query(Incident).filter(Incident.id == incident_id).first()
            project_id = str(incident.project_id) if incident and incident.project_id else None
        finally:
            db.close()

        project_config = get_project_config(project_id) if project_id else {}

        engine = VerificationEngine(project_config=project_config)
        success = engine.verify(
            incident_id=incident_id,
            audit_id=audit_id,
            summary_text=summary_text,
            diagnosis=diagnosis,
            action_taken=action_taken,
            pod_name=pod_name,
            baseline_metrics=baseline_metrics,
        )

        if success:
            logger.info(
                f"[TRACE:{incident_id}] === [TASK 5: VERIFICATION] DONE → RESOLVED ==="
            )
        else:
            logger.warning(
                f"[TRACE:{incident_id}] === [TASK 5: VERIFICATION] DONE → FAILED ==="
            )

        return success

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 5 FAILED] {e}")
        return False


# ════════════════════════════════════════════════════════════
# Utility
# ════════════════════════════════════════════════════════════

def _retry_or_fail(task, db, incident_id: str, exc: Exception, stage: str):
    """Retries the task, or marks the incident FAILED once retries are exhausted.

    Celery's retry(exc=...) re-raises `exc` itself (not MaxRetriesExceededError)
    when the limit is hit, so the limit is checked here before retrying.
    """
    if task.max_retries is not None and task.request.retries >= task.max_retries:
        _mark_failed(db, incident_id, reason=f"{exc.__class__.__name__}: {exc}", stage=stage)
        return False
    raise task.retry(exc=exc, countdown=2 ** task.request.retries)


def _mark_failed(db, incident_id: str, reason: str = None, stage: str = None):
    """Safely marks an incident as FAILED, recording why in raw_payload_cache."""
    try:
        db.rollback()
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if incident and incident.status not in (
            IncidentStatus.FAILED,
            IncidentStatus.RESOLVED,
        ):
            if reason:
                payload = dict(incident.raw_payload_cache or {})
                payload["pipeline_failure"] = {
                    "stage": stage,
                    "error": reason,
                    "failed_at": time.time(),
                }
                incident.raw_payload_cache = payload
            try:
                incident.transition_to(IncidentStatus.FAILED)
            except ValueError:
                # e.g. ESCALATED has no modelled → FAILED edge; keep forcing it as before.
                incident.status = IncidentStatus.FAILED
            db.commit()
            emit_sync("incident.status_changed", {"new_status": "FAILED"}, str(incident.id))
            logger.critical(
                f"[TRACE:{incident_id}] [FATAL] Incident marked FAILED"
                f"{f' in {stage}' if stage else ''}: {reason or 'no reason recorded'}"
            )
    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] Failed to mark FAILED: {e}")


# ════════════════════════════════════════════════════════════
# Stage 2: AI patch proposals (proposal only — never applied)
# ════════════════════════════════════════════════════════════

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _patch_proposals_auto() -> bool:
    from engine.patch_routing import auto_generation_enabled
    return auto_generation_enabled()


def _route_patch_proposal(incident_id: str):
    """After AI diagnosis: record the confidence route and, on the automatic route, queue one
    proposal run. Low-confidence incidents only get a PENDING decision (a human must approve).
    Best effort: a failure here never changes the incident or the diagnosis result."""
    if not _patch_proposals_auto():
        return
    from engine.patch_routing import request_generation
    from engine.routing_policy import RoutingError

    def dispatch(inc_id, decision_id, claim):
        generate_patch_proposal.apply_async(args=[inc_id, decision_id, claim])

    try:
        result = request_generation(SessionLocal, uuid.UUID(incident_id), dispatch, trigger="diagnosis")
        logger.info(f"[TRACE:{incident_id}] [PATCH ROUTING] {result['decision']['routing_mode']} "
                    f"(started={result['started']})")
    except RoutingError as exc:
        logger.info(f"[TRACE:{incident_id}] [PATCH ROUTING] not started: {exc.code}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[TRACE:{incident_id}] [PATCH ROUTING] skipped: {exc.__class__.__name__}")


@celery_app.task(bind=True, max_retries=0, time_limit=600, soft_time_limit=540)
def generate_patch_proposal(self, incident_id: str, decision_id: str, claim: str = None):
    """Runs the patch proposer for an authorised routing decision. Never applies anything:
    the result is a stored proposal (VALIDATED / NOT_GENERATED / REJECTED / FAILED)."""
    from engine.patch_routing import run_generation

    logger.info(f"[TRACE:{incident_id}] === [STAGE 2: PATCH PROPOSAL] START (decision {decision_id}) ===")
    result = run_generation(SessionLocal, incident_id, decision_id, claim, _REPO_ROOT)
    emit_sync("incident.patch_proposal", {"status": result.get("status"), "reason_code": result.get("reason_code"),
                                          "proposal_id": result.get("proposal_id")}, incident_id)
    logger.info(f"[TRACE:{incident_id}] === [STAGE 2: PATCH PROPOSAL] DONE → {result.get('status')}"
                f"/{result.get('reason_code')} (not applied) ===")
    return result


# ════════════════════════════════════════════════════════════
# STAGES 3-4: approved remediation (PR -> deployment -> verification)
# ════════════════════════════════════════════════════════════

def _schedule_remediation(run_id: str, delay_seconds: int):
    advance_remediation.apply_async(args=[run_id], countdown=max(0, int(delay_seconds)))


@celery_app.task(bind=True, max_retries=0, time_limit=900, soft_time_limit=840)
def advance_remediation(self, run_id: str):
    """Runs the next step of an approved remediation run. Each step re-checks its preconditions,
    so duplicate or late deliveries are harmless; follow-up steps are scheduled by the workflow."""
    from engine import remediation_workflow as wf

    result = wf.advance(SessionLocal, uuid.UUID(str(run_id)), wf.default_deps(_schedule_remediation))
    incident_id = result.get("incident_id")
    logger.info(f"[TRACE:{incident_id}] [REMEDIATION] run {run_id} stage={result.get('stage')} "
                f"blocked={result.get('blocked_code')}")
    if incident_id:
        emit_sync("incident.remediation", {"stage": result.get("stage"), "blocked_code": result.get("blocked_code")},
                  incident_id)
    return {"stage": result.get("stage"), "blocked_code": result.get("blocked_code")}
