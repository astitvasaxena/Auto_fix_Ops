import os
import hashlib
import uuid
import time
from typing import Optional
from fastapi import FastAPI, Depends, Request, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from datetime import datetime, timedelta
import asyncio

from .database import init_relational_db, get_db, incident_contexts_collection, logger
from .models import (
    Incident, IncidentStatus, RemediationAudit, ExecutionStatus,
    ProjectConfig, SystemConfig, SystemMode, ServiceAccount
)
from .schemas import AlertmanagerPayload
from .config_helpers import (
    get_project_config, invalidate_config_cache, get_default_project_id,
    get_system_mode, set_system_mode, is_system_disabled,
    invalidate_service_account_cache
)
from workers.tasks import build_incident_context, execute_remediation
from .events import ws_manager, redis_listener, emit_sync
from .patch_proposals import router as patch_proposals_router
from .remediation import router as remediation_router

app = FastAPI(title="AutoFixOps API Gateway")
app.include_router(patch_proposals_router)   # Stage 2: AI patch proposals (proposal only, never applied)
app.include_router(remediation_router)       # Stages 3-4: approval, GitHub PR, controlled deployment, verification

# CORS — allow dashboard direct access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_redis_listener_task = None


@app.on_event("startup")
async def on_startup():
    global _redis_listener_task
    logger.info("Initializing relational database schema...")
    init_relational_db()
    
    # Start Redis WebSocket listener
    _redis_listener_task = asyncio.create_task(redis_listener(ws_manager))
    
    logger.info("AutoFixOps API Gateway started successfully.")


@app.on_event("shutdown")
async def on_shutdown():
    global _redis_listener_task
    if _redis_listener_task and not _redis_listener_task.done():
        _redis_listener_task.cancel()
        try:
            await _redis_listener_task
        except asyncio.CancelledError:
            pass
    logger.info("AutoFixOps API Gateway stopped.")


# Statuses that end an incident for ingestion de-duplication purposes.
# HUMAN_APPROVED / HUMAN_REJECTED are inert legacy values (feature removed);
# rows already in them can never move again, so they must count as closed.
_CLOSED_STATUSES = [
    IncidentStatus.RESOLVED, IncidentStatus.FAILED,
    IncidentStatus.HUMAN_APPROVED, IncidentStatus.HUMAN_REJECTED,
]


def _incident_source(incident) -> str:
    """How the incident was created: "chaos_injection" (POST /chaos/inject creates it directly,
    without Prometheus/Alertmanager) or "alertmanager" (webhook from a firing alert rule)."""
    return "chaos_injection" if (incident.raw_payload_cache or {}).get("chaos") else "alertmanager"


def generate_dedup_key(alert: dict, window_seconds: int = 120) -> str:
    """Generates a time-bucketed deduplication fingerprint."""
    labels = alert.get("labels", {})
    alertname = labels.get("alertname", "Unknown")
    namespace = labels.get("namespace", "default")
    pod = labels.get("pod", "unknown")
    container = labels.get("container", "unknown")
    severity = labels.get("severity", "unknown")

    bucket = int(time.time() / window_seconds)
    raw_key = f"{alertname}-{namespace}-{pod}-{container}-{severity}-{bucket}"
    return hashlib.md5(raw_key.encode()).hexdigest()


# ════════════════════════════════════════════════════════════
# ENDPOINT 1: Webhook Ingestion (multi-tenant)
# ════════════════════════════════════════════════════════════

@app.post("/api/v1/alerts")
async def receive_alert(
    payload: AlertmanagerPayload, request: Request, db: Session = Depends(get_db)
):
    logger.info("=== [INGEST] Received webhook from Alertmanager ===")

    # System kill switch check
    if is_system_disabled():
        raise HTTPException(status_code=503, detail="System is DISABLED. No alerts accepted.")

    # Get or create default project
    default_project_id = get_default_project_id()
    processed_incidents = []

    for alert in payload.alerts:
        status = alert.get("status", "firing")
        dedup_key = generate_dedup_key(alert)
        alert_name = alert.get("labels", {}).get("alertname", "UnknownAlert")

        if status == "resolved":
            incident = (
                db.query(Incident)
                .filter(
                    Incident.alert_name == alert_name,
                    Incident.status.notin_(_CLOSED_STATUSES),
                    Incident.project_id == default_project_id,
                )
                .order_by(Incident.created_at.desc())
                .first()
            )
            if incident:
                logger.info(
                    f"[ALERTMANAGER RESOLVED] Received resolved webhook for active incident {incident.id} "
                    f"({alert_name}, current status: {incident.status.value})."
                )
            else:
                logger.info(f"[ALERTMANAGER RESOLVED] No active incident found for {alert_name}.")
            continue

        # Prevent duplicate processing of the same active incident
        active_incident = (
            db.query(Incident)
            .filter(
                Incident.alert_name == alert_name,
                Incident.status.notin_(_CLOSED_STATUSES),
                Incident.project_id == default_project_id,
            )
            .order_by(Incident.created_at.desc())
            .first()
        )
        if active_incident:
            logger.info(
                f"[DEDUP] Active incident {active_incident.id} already in progress for "
                f"alert '{alert_name}' (status: {active_incident.status.value}). "
                f"Skipping duplicate creation."
            )
            processed_incidents.append(str(active_incident.id))
            continue

        severity = alert.get("labels", {}).get("severity", "unknown")
        incident = Incident(
            alert_fingerprint=dedup_key,
            alert_name=alert_name,
            severity=severity,
            status=IncidentStatus.INGESTED,
            raw_payload_cache=alert,
            project_id=default_project_id,
        )

        try:
            db.add(incident)
            db.commit()
            db.refresh(incident)
            logger.info(f"=== [DB WRITE] Created Incident: {incident.id} ===")
            
            try:
                emit_sync("incident.created", {"alert_name": alert_name, "severity": severity}, str(incident.id))
            except Exception as e:
                logger.error(f"[WS] Failed to emit incident.created: {e}")
                
        except IntegrityError:
            db.rollback()
            logger.warning(f"[DB WRITE IGNORED] Deduplicated: {dedup_key}")
            continue

        logger.info(f"=== [QUEUE PUSH] Dispatching for Incident {incident.id} ===")
        task = build_incident_context.apply_async(args=[str(incident.id)])
        logger.info(f"[TRACE:{incident.id}] Task enqueued. Job ID: {task.id}")
        processed_incidents.append(str(incident.id))

    return {"status": "accepted", "processed_incidents": processed_incidents}


# ════════════════════════════════════════════════════════════
# ENDPOINT 2: List Incidents (filterable)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/incidents")
def list_incidents(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(Incident).order_by(Incident.created_at.desc())

    if status:
        try:
            status_enum = IncidentStatus(status.upper())
            query = query.filter(Incident.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    incidents = query.limit(limit).all()

    return {
        "count": len(incidents),
        "incidents": [
            {
                "id": str(i.id),
                "status": i.status.value,
                "alert_name": i.alert_name,
                "severity": i.severity,
                "diagnosis": i.diagnosis_classification,
                "confidence": i.diagnosis_confidence,
                "diagnosed_by": i.diagnosed_by,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
                "source": _incident_source(i),
                "status_timeline": i.status_timeline or [],
            }
            for i in incidents
        ],
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 3: Incident Detail + Context
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/incidents/{incident_id}/context")
def get_incident_context(incident_id: uuid.UUID, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    incident_id = str(incident.id)

    # Fetch MongoDB context
    mongo_ctx = incident_contexts_collection.find_one(
        {"incident_id": incident_id}, {"_id": 0}
    )

    # Fetch remediation audits
    audits = (
        db.query(RemediationAudit)
        .filter(RemediationAudit.incident_id == incident.id)
        .all()
    )

    return {
        "incident": {
            "id": str(incident.id),
            "status": incident.status.value,
            "alert_name": incident.alert_name,
            "severity": incident.severity,
            "diagnosis": incident.diagnosis_classification,
            "confidence": incident.diagnosis_confidence,
            "reasoning": incident.diagnosis_reasoning,
            "diagnosed_by": incident.diagnosed_by,
            "ai_report": incident.ai_report,
            "policy_decision": (incident.raw_payload_cache or {}).get("policy_decision"),
            "resolved_target": incident.resolved_target,
            "created_at": incident.created_at.isoformat() if incident.created_at else None,
            "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
            "source": _incident_source(incident),
            "status_timeline": incident.status_timeline or [],
        },
        "telemetry_context": mongo_ctx,
        "remediation_audits": [
            {
                "id": str(a.id),
                "action": a.proposed_action,
                "policy_verdict": a.policy_verdict,
                "pr_url": a.github_pr_url,
                "execution_status": a.execution_status.value if a.execution_status else None,
                "is_shadow": a.is_shadow_run,
                "human_agreed": a.human_agreed,
                "failure_reason": a.failure_reason,
                "failure_root_cause": a.failure_root_cause.value if a.failure_root_cause else None,
            }
            for a in audits
        ],
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 4: Human Approval (Escalation Override)
# ════════════════════════════════════════════════════════════

@app.post("/api/v1/escalations/{incident_id}/approve")
def approve_escalation(incident_id: uuid.UUID, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    incident_id = str(incident.id)

    if incident.status != IncidentStatus.ESCALATED:
        raise HTTPException(
            status_code=409,
            detail=f"Incident is in state {incident.status.value}, not ESCALATED.",
        )

    # An incident escalated by a Stage 3 rejection must not be remediated through this legacy,
    # unvalidated path; it can only be closed by a human (POST /api/v1/incidents/{id}/close).
    from .models import RemediationRun
    if db.query(RemediationRun).filter(RemediationRun.incident_id == incident.id).first():
        raise HTTPException(
            status_code=409,
            detail="REMEDIATION_REJECTED: a human rejected this incident's validated proposal; "
                   "close the incident instead of using the legacy escalation path.",
        )

    # Build verdict from stored diagnosis
    verdict = {
        "root_cause_classification": incident.diagnosis_classification,
        "confidence": incident.diagnosis_confidence or 1.0,
        "reasoning": incident.diagnosis_reasoning or "",
        "action": {
            "type": (
                "INCREASE_MEMORY_LIMIT"
                if "MEMORY" in (incident.diagnosis_classification or "")
                else "RESTART_POD"
            ),
            "target": incident.raw_payload_cache.get("labels", {}).get("pod", "unknown"),
            "patch_value": "256Mi",
        },
    }

    incident.transition_to(IncidentStatus.POLICY_APPROVED)
    db.commit()

    logger.info(f"[TRACE:{incident.id}] Human approved escalated incident.")
    execute_remediation.apply_async(args=[str(incident.id), verdict])

    return {
        "status": "approved",
        "incident_id": str(incident.id),
        "message": "Incident approved by human. Remediation dispatched.",
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 5: Pipeline Metrics (with temporal windows)
# ════════════════════════════════════════════════════════════

def _compute_metrics(db: Session, since: Optional[datetime] = None) -> dict:
    """Computes aggregate pipeline health metrics, optionally filtered by time."""
    base_q = db.query(Incident)
    audit_q = db.query(RemediationAudit)

    if since:
        base_q = base_q.filter(Incident.created_at >= since)
        audit_q = audit_q.filter(RemediationAudit.created_at >= since)

    total = base_q.count()
    if total == 0:
        return {"total_incidents": 0, "message": "No incidents in this window."}

    resolved = base_q.filter(Incident.status == IncidentStatus.RESOLVED).count()
    failed = base_q.filter(Incident.status == IncidentStatus.FAILED).count()
    escalated = base_q.filter(Incident.status == IncidentStatus.ESCALATED).count()
    by_rule = base_q.filter(Incident.diagnosed_by == "RULE_ENGINE").count()
    by_ai = base_q.filter(Incident.diagnosed_by == "AI_ENGINE").count()

    total_audits = audit_q.count()
    approved = audit_q.filter(RemediationAudit.policy_verdict == "APPROVED").count()
    shadow = audit_q.filter(RemediationAudit.is_shadow_run == "true").count()
    v_pass = audit_q.filter(
        RemediationAudit.execution_status == ExecutionStatus.VERIFICATION_PASSED
    ).count()
    v_fail = audit_q.filter(
        RemediationAudit.execution_status == ExecutionStatus.VERIFICATION_FAILED
    ).count()

    return {
        "total_incidents": total,
        "resolved": resolved,
        "failed": failed,
        "escalated": escalated,
        "rule_engine_hit_rate": round(by_rule / total, 3) if total else 0,
        "ai_fallback_rate": round(by_ai / total, 3) if total else 0,
        "policy_approval_rate": round(approved / total_audits, 3) if total_audits else 0,
        "escalation_rate": round(escalated / total, 3) if total else 0,
        "verification_success_rate": (
            round(v_pass / (v_pass + v_fail), 3)
            if (v_pass + v_fail) > 0
            else 0
        ),
        "shadow_runs": shadow,
    }


@app.get("/api/v1/metrics")
def get_metrics(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    return {
        "all_time": _compute_metrics(db),
        "last_24h": _compute_metrics(db, since=now - timedelta(hours=24)),
        "last_7d": _compute_metrics(db, since=now - timedelta(days=7)),
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 6: System Status (top bar indicator)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/status")
def get_system_status(db: Session = Depends(get_db)):
    from engine.circuit_breaker import get_circuit_breaker_registry
    from api.config_helpers import get_github_credentials

    # Get default project config
    config = db.query(ProjectConfig).first()
    registry = get_circuit_breaker_registry()
    system_mode = get_system_mode()
    
    github_creds = get_github_credentials(str(config.id) if config else None)

    return {
        "system_mode": system_mode,
        "shadow_mode": config.shadow_mode if config else "true",
        "circuit_breaker": registry.global_breaker.state,
        "circuit_breaker_states": registry.get_all_states(),
        "github_connected": bool(github_creds.get("token")),
        "prometheus_url": config.prometheus_url if config else None,
        "target_namespace": config.target_namespace if config else "autofixops",
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 6b: Prometheus Health Check (proxy for dashboard)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/prometheus/health")
async def check_prometheus_health(db: Session = Depends(get_db)):
    """Proxies a health check to Prometheus (browser can't call directly due to CORS)."""
    config = db.query(ProjectConfig).first()
    prom_url = (
        (config.prometheus_url if config else None)
        or os.getenv("PROMETHEUS_URL", "")
    )
    if not prom_url:
        raise HTTPException(status_code=404, detail="No Prometheus URL configured.")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{prom_url}/-/healthy")
        if resp.status_code == 200:
            return {"status": "healthy", "prometheus_url": prom_url}
        raise HTTPException(status_code=502, detail=f"Prometheus returned HTTP {resp.status_code}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Prometheus health check timed out.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach Prometheus: {e}")


@app.get("/api/v1/target/health")
async def check_target_health(db: Session = Depends(get_db)):
    """Target app HEALTHY/DOWN from Prometheus' `up` series (same signal as the TargetAppDown alert)."""
    config = db.query(ProjectConfig).first()
    prom_url = (
        (config.prometheus_url if config else None)
        or os.getenv("PROMETHEUS_URL", "")
    )
    if not prom_url:
        return {"status": "UNKNOWN", "detail": "No Prometheus URL configured."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{prom_url.rstrip('/')}/api/v1/query",
                params={"query": 'up{job="autofixops-target"}'},
            )
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if not results:
            return {"status": "UNKNOWN", "detail": "No scrape data for target."}
        up = any(float(r["value"][1]) == 1 for r in results)
        return {"status": "HEALTHY" if up else "DOWN"}
    except Exception as e:
        return {"status": "UNKNOWN", "detail": f"Failed to query Prometheus: {e}"}


# ════════════════════════════════════════════════════════════
# ENDPOINT 7: System Mode (Kill Switch)
# ════════════════════════════════════════════════════════════

@app.post("/api/v1/system/mode")
async def update_system_mode(request: Request):
    body = await request.json()
    mode = body.get("mode", "").upper()
    reason = body.get("reason", "")

    if mode not in ("ACTIVE", "SHADOW", "DISABLED"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}. Use ACTIVE, SHADOW, or DISABLED.")

    set_system_mode(mode, reason)
    return {"status": "updated", "system_mode": mode}


@app.get("/api/v1/system/mode")
async def get_system_mode_endpoint():
    return {"system_mode": get_system_mode()}


# ════════════════════════════════════════════════════════════
# ENDPOINT 8: Project Configuration (multi-tenant)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/config")
def get_config(db: Session = Depends(get_db)):
    config = db.query(ProjectConfig).first()
    if not config:
        return {"configured": False}
    return {
        "configured": True,
        "project_id": str(config.id),
        "name": config.name,
        "github_repo": config.github_repo,
        "prometheus_url": config.prometheus_url,
        "target_namespace": config.target_namespace,
        "target_manifest_path": config.target_manifest_path,
        "shadow_mode": config.shadow_mode,
        "confidence_threshold": config.confidence_threshold,
        "allowed_chaos_namespaces": config.allowed_chaos_namespaces,
        "max_resource_scale_factor": config.max_resource_scale_factor,
    }


@app.post("/api/v1/config")
async def save_config(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    config = db.query(ProjectConfig).first()

    if not config:
        config = ProjectConfig(name=body.get("name", "Default Project"))
        db.add(config)

    config.name = body.get("name", config.name)
    config.github_repo = body.get("github_repo", config.github_repo)
    config.prometheus_url = body.get("prometheus_url", config.prometheus_url)
    config.target_namespace = body.get("target_namespace", config.target_namespace)
    config.target_manifest_path = body.get("target_manifest_path", config.target_manifest_path)
    config.shadow_mode = body.get("shadow_mode", config.shadow_mode)
    config.confidence_threshold = body.get("confidence_threshold", config.confidence_threshold)

    # Safety bounds
    if "allowed_chaos_namespaces" in body:
        config.allowed_chaos_namespaces = body["allowed_chaos_namespaces"]
    if "max_resource_scale_factor" in body:
        config.max_resource_scale_factor = min(body["max_resource_scale_factor"], 5.0)

    # Token setting has been removed since we only use the system-wide bot account.

    db.commit()
    db.refresh(config)

    # Invalidate cache
    invalidate_config_cache(str(config.id))

    logger.info(f"[CONFIG] Project configuration saved (id={config.id}).")
    return {"status": "saved", "project_id": str(config.id)}


# ════════════════════════════════════════════════════════════
# ENDPOINT 9: Chaos Injection (per-project rate limit + namespace enforcement)
# ════════════════════════════════════════════════════════════

import httpx

CHAOS_RATE_LIMIT: dict = {}  # {project_id:fault_type: timestamp}
CHAOS_COOLDOWN_SECONDS = 60
CPU_CHAOS_TASKS: set = set()


async def _run_blocking_cpu_chaos(url: str):
    """Calls the target's intentionally non-returning CPU endpoint in background."""
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            await client.get(url)
    except Exception as e:
        logger.warning(f"[CHAOS] CPU spike request ended unexpectedly: {e}")


@app.post("/api/v1/chaos/inject")
async def inject_chaos(request: Request, db: Session = Depends(get_db)):
    # Kill switch check
    if is_system_disabled():
        raise HTTPException(status_code=503, detail="System is DISABLED. Chaos injection blocked.")

    body = await request.json()
    fault_type = body.get("fault_type")  # "memory_leak", "cpu_spike", "crash_loop"
    target_url = body.get("target_url")  # e.g. "http://target-app:8000"
    confirmation = body.get("confirmation")  # Must be "CONFIRM"

    if confirmation != "CONFIRM":
        raise HTTPException(status_code=400, detail="Must type CONFIRM to inject chaos.")

    # Get project config
    config = db.query(ProjectConfig).first()
    project_id = str(config.id) if config else "global"
    namespace = config.target_namespace if config else "autofixops"

    # Namespace enforcement — hard block production
    allowed_namespaces = config.allowed_chaos_namespaces if config else ["staging", "test", "dev", "default", "autofixops"]
    if namespace not in (allowed_namespaces or []):
        raise HTTPException(
            status_code=403,
            detail=f"Chaos injection blocked: namespace '{namespace}' is not in allowed list {allowed_namespaces}.",
        )

    # Per-project + per-fault rate limit
    rate_key = f"{project_id}:{fault_type}"
    now = time.time()
    last = CHAOS_RATE_LIMIT.get(rate_key, 0)
    remaining = CHAOS_COOLDOWN_SECONDS - int(now - last)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Wait {remaining}s before next injection.",
            headers={"Retry-After": str(remaining)},
        )
    CHAOS_RATE_LIMIT[rate_key] = now

    # Route to target app endpoint
    endpoint_map = {
        "memory_leak": "/leak",
        "cpu_spike": "/cpu",
        "crash_loop": "/_chaos/crash",
    }
    endpoint = endpoint_map.get(fault_type)
    if not endpoint:
        raise HTTPException(status_code=400, detail=f"Unknown fault type: {fault_type}")

    if not target_url:
        raise HTTPException(status_code=400, detail="target_url is required.")

    # Clean up target_url if it has trailing slash or /chaos
    target_url = target_url.rstrip("/").replace("/_chaos", "").replace("/chaos", "")

    try:
        url = f"{target_url}{endpoint}"
        if fault_type == "cpu_spike":
            # /cpu deliberately never returns, so validate the reachable target
            # first and then let the existing GET run independently.
            async with httpx.AsyncClient(timeout=5.0) as client:
                health = await client.get(f"{target_url}/health")
                health.raise_for_status()
            task = asyncio.create_task(_run_blocking_cpu_chaos(url))
            CPU_CHAOS_TASKS.add(task)
            task.add_done_callback(CPU_CHAOS_TASKS.discard)
            response_code = 202
        else:
            if fault_type == "memory_leak":
                url += "?mb=50"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
            response_code = resp.status_code
        logger.info(f"[CHAOS] Injected {fault_type} via {url} → {response_code}")
    except Exception as e:
        CHAOS_RATE_LIMIT.pop(rate_key, None)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach target at {url}: {str(e)}. Target application on port 8001 may be offline or stopped."
        )

    # Create an incident so the remediation pipeline picks it up
    fault_to_alert = {
        "memory_leak": ("ChaosInjection:MemoryLeak", "HIGH"),
        "cpu_spike":   ("ChaosInjection:CPUSpike",   "MEDIUM"),
        "crash_loop":  ("ChaosInjection:CrashLoop",  "HIGH"),
    }
    alert_name, severity = fault_to_alert[fault_type]
    dedup_key = hashlib.md5(
        f"chaos-{fault_type}-{project_id}-{int(time.time() / 120)}".encode()
    ).hexdigest()

    incident = Incident(
        alert_fingerprint=dedup_key,
        alert_name=alert_name,
        severity=severity,
        status=IncidentStatus.INGESTED,
        raw_payload_cache={
            "chaos": True,
            "fault_type": fault_type,
            "target_url": target_url,
            "labels": {
                "alertname": alert_name,
                "namespace": namespace,
                "severity": severity.lower(),
            },
        },
        project_id=config.id if config else None,
    )
    try:
        db.add(incident)
        db.commit()
        db.refresh(incident)
        logger.info(f"[CHAOS] Created Incident {incident.id} for {fault_type}")
        try:
            emit_sync("incident.created", {"alert_name": alert_name, "severity": severity}, str(incident.id))
        except Exception as e:
            logger.error(f"[WS] Failed to emit incident.created for chaos: {e}")
        build_incident_context.apply_async(args=[str(incident.id)])
    except IntegrityError:
        db.rollback()
        logger.warning(f"[CHAOS] Deduplicated incident for {fault_type}")

    return {
        "status": "injected",
        "incident_id": str(incident.id),
        "fault_type": fault_type,
        "target": f"{target_url}{endpoint}",
        "response_code": response_code,
        "cooldown_seconds": CHAOS_COOLDOWN_SECONDS,
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 10: Patch Rollback
# ════════════════════════════════════════════════════════════

@app.post("/api/v1/incidents/{incident_id}/rollback")
def rollback_patch(incident_id: str, db: Session = Depends(get_db)):
    """Initiates a rollback using the stored previous_values from the remediation audit."""
    audit = (
        db.query(RemediationAudit)
        .filter(RemediationAudit.incident_id == incident_id)
        .order_by(RemediationAudit.created_at.desc())
        .first()
    )

    if not audit:
        raise HTTPException(status_code=404, detail="No remediation audit found for this incident.")

    if not audit.previous_values:
        raise HTTPException(status_code=409, detail="No previous values stored — rollback not possible.")

    project_config = get_project_config(str(audit.project_id)) if audit.project_id else {}
    from engine.remediation import RemediationEngine
    engine = RemediationEngine(project_config)

    try:
        result = engine.execute_rollback(incident_id, audit.previous_values)
        
        try:
            from .events import emit_sync
            emit_sync("remediation.pr_created", {"pr_url": result.get("pr_url"), "is_shadow": False}, incident_id)
        except Exception as e:
            logger.error(f"[WS] Failed to emit rollback pr_created: {e}")
            
        return {
            "status": "rollback_created",
            "incident_id": incident_id,
            "pr_url": result.get("pr_url"),
            "message": "Rollback PR has been created successfully.",
        }
    except Exception as e:
        logger.error(f"[ROLLBACK ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create rollback PR: {e}")


# ════════════════════════════════════════════════════════════
# ENDPOINT 11: Service Account Management
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/service-account")
def get_service_account(db: Session = Depends(get_db)):
    """Returns the current service account configuration (token is masked)."""
    sa = db.query(ServiceAccount).filter(ServiceAccount.id == "github").first()
    if not sa:
        return {"configured": False}
    return {
        "configured": True,
        "display_name": sa.display_name,
        "github_username": sa.github_username,
        "github_token": sa.get_masked_token(),
        "is_active": sa.is_active,
        "created_at": sa.created_at.isoformat() if sa.created_at else None,
        "updated_at": sa.updated_at.isoformat() if sa.updated_at else None,
    }


@app.post("/api/v1/service-account")
async def save_service_account(request: Request, db: Session = Depends(get_db)):
    """
    Creates or updates the system-level GitHub service account.
    Validates the token against GitHub API and auto-discovers the username.
    """
    body = await request.json()
    token = body.get("github_token", "").strip()
    display_name = body.get("display_name", "AutoFixOps Bot")

    if not token:
        raise HTTPException(status_code=400, detail="github_token is required.")

    # Validate the token against GitHub API
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
            )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"GitHub token validation failed (HTTP {resp.status_code}). "
                       f"Ensure the token has 'repo' scope.",
            )
        github_user = resp.json()
        github_username = github_user.get("login", "")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GitHub API timed out during token validation.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to validate token: {e}")

    # Upsert the service account
    sa = db.query(ServiceAccount).filter(ServiceAccount.id == "github").first()
    if not sa:
        sa = ServiceAccount(id="github")
        db.add(sa)

    sa.display_name = display_name
    sa.github_username = github_username
    sa.set_github_token(token)
    sa.is_active = "true"
    db.commit()
    db.refresh(sa)

    # Invalidate cache
    invalidate_service_account_cache()

    logger.info(f"[SERVICE ACCOUNT] Configured: {github_username} ({display_name})")
    return {
        "status": "saved",
        "github_username": github_username,
        "display_name": display_name,
        "message": f"Service account configured as @{github_username}",
    }


@app.delete("/api/v1/service-account")
def delete_service_account(db: Session = Depends(get_db)):
    """Removes the service account. System falls back to .env token."""
    sa = db.query(ServiceAccount).filter(ServiceAccount.id == "github").first()
    if not sa:
        raise HTTPException(status_code=404, detail="No service account configured.")

    db.delete(sa)
    db.commit()

    # Invalidate cache
    invalidate_service_account_cache()

    logger.info("[SERVICE ACCOUNT] Deleted. Falling back to .env token.")
    return {"status": "deleted", "message": "Service account removed. System will use .env fallback."}


# ════════════════════════════════════════════════════════════
# ENDPOINT 13: Incident Debug Trace
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/incidents/{incident_id}/debug")
def get_incident_debug(incident_id: uuid.UUID, db: Session = Depends(get_db)):
    """Returns the pre-diagnosis raw context, AI-prepared context, and diagnosis trace.

    Includes:
      - raw_context: the original context built before diagnosis (Task 1).
      - ai_ready_context: the context prepared for the AI with RAG & prompt format (Task 2).
      - debug_available: whether the requested data actually exists.
      - messages: status explanations if any part of the data is pending or missing.
      - teacher_summary: clear, readable comparison of pre-diagnosis vs post-diagnosis.
    """
    from engine.debug_trace import load_trace, is_debug_enabled

    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    incident_id = str(incident.id)   # string form for trace files and the context store

    raw_payload = incident.raw_payload_cache or {}
    trace = load_trace(incident_id)

    # 1. Resolve raw_context (captured in Task 1 before diagnosis)
    raw_context = raw_payload.get("raw_context")
    if not raw_context and trace:
        raw_context = trace.get("raw_context") or (
            trace.get("stages", {}).get("build_context", {}).get("context")
        )

    # Fallback to MongoDB if present
    mongo_ctx = None
    try:
        mongo_ctx = incident_contexts_collection.find_one(
            {"incident_id": incident_id}, {"_id": 0}
        )
    except Exception:
        pass

    if not raw_context and mongo_ctx:
        raw_context = mongo_ctx

    # 2. Resolve ai_ready_context (assembled for the LLM)
    ai_ready_context = None
    if trace and "context" in trace.get("stages", {}):
        ai_ready_context = trace["stages"]["context"].get("incident_context")
    elif incident.ai_report and isinstance(incident.ai_report, dict):
        ai_ready_context = incident.ai_report.get("incident_context")

    debug_available = bool(raw_context or ai_ready_context or trace)

    # 3. Informative status messages
    messages = {}
    if not raw_context:
        messages["raw_context"] = "Raw context not found. Incident may still be in INGESTED state or context collection failed."
    else:
        messages["raw_context"] = "Successfully captured in build_incident_context before diagnosis began."

    if not ai_ready_context:
        if incident.status == IncidentStatus.CONTEXT_BUILT:
            messages["ai_ready_context"] = "AI diagnosis has not run yet. Incident is currently in CONTEXT_BUILT state."
        elif incident.status == IncidentStatus.INGESTED:
            messages["ai_ready_context"] = "Pipeline has not reached AI diagnosis stage yet."
        else:
            messages["ai_ready_context"] = "AI analysis context not recorded (resolved by rule engine or legacy fallback)."
    else:
        messages["ai_ready_context"] = "Successfully assembled with runbook RAG context and processed by the LLM."

    if not trace:
        messages["trace"] = "Trace file not found on disk. Ensure DEBUG_PROMPTS=true is in .env and Celery worker was restarted."
    else:
        messages["trace"] = "Full prompt trace loaded from debug_traces/<incident_id>.json."

    # 4. Clean teacher summary
    fault_type = (
        raw_payload.get("fault_type")
        or (raw_context.get("chaos", {}) if isinstance(raw_context, dict) else {}).get("fault_type", "unavailable")
    )
    target_pod = (
        (raw_context.get("target", {}) if isinstance(raw_context, dict) else {}).get("pod")
        or (raw_context.get("target_pod") if isinstance(raw_context, dict) else "unavailable")
    )
    telemetry_present = (
        (raw_context.get("telemetry", {}) if isinstance(raw_context, dict) else {}).get("cpu_available", False)
        if isinstance(raw_context, dict) else False
    )

    teacher_summary = {
        "incident_id": str(incident.id),
        "current_status": incident.status.value,
        "phase_1_before_diagnosis": {
            "stage_name": "build_incident_context",
            "captured_before_ai": bool(raw_context),
            "alert_name": incident.alert_name,
            "severity": incident.severity,
            "fault_type": fault_type,
            "target_pod": target_pod,
            "target_url": raw_payload.get("target_url") or "unavailable",
            "telemetry_collected": telemetry_present,
            "raw_context_persisted": bool(raw_context),
        },
        "phase_2_ai_diagnosis": {
            "stage_name": "diagnose_incident",
            "diagnosis_status": "COMPLETED" if incident.status not in (IncidentStatus.INGESTED, IncidentStatus.CONTEXT_BUILT) else "PENDING",
            "diagnosed_by": incident.diagnosed_by or "pending",
            "classification": incident.diagnosis_classification or "pending",
            "confidence": incident.diagnosis_confidence,
            "has_ai_report": bool(incident.ai_report),
            "proposed_action": (incident.ai_report or {}).get("proposed_action", {}).get("type") if incident.ai_report else "pending",
        },
    }

    return {
        "incident_id": str(incident.id),
        "status": incident.status.value,
        "debug_available": debug_available,
        "debug_prompts_enabled": is_debug_enabled(),
        "messages": messages,
        "teacher_summary": teacher_summary,
        "raw_context": raw_context,
        "ai_ready_context": ai_ready_context,
        "trace": trace,
        "db_summary": {
            "status": incident.status.value,
            "diagnosed_by": incident.diagnosed_by,
            "classification": incident.diagnosis_classification,
            "confidence": incident.diagnosis_confidence,
            "reasoning": incident.diagnosis_reasoning,
            "ai_report": incident.ai_report,
            "status_timeline": incident.status_timeline,
        },
        "telemetry_context": mongo_ctx or (raw_context.get("metrics") if isinstance(raw_context, dict) else None),
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 14: Dedicated Pre-Diagnosis Context Viewer
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/incidents/{incident_id}/pre-diagnosis-context")
def get_pre_diagnosis_context(incident_id: uuid.UUID, db: Session = Depends(get_db)):
    """Simple, dedicated endpoint to inspect only the pre-diagnosis incident context."""
    from engine.debug_trace import load_trace

    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    incident_id = str(incident.id)   # string form for trace files and the context store

    raw_payload = incident.raw_payload_cache or {}
    raw_context = raw_payload.get("raw_context")
    if not raw_context:
        trace = load_trace(incident_id)
        if trace:
            raw_context = trace.get("raw_context")

    if not raw_context:
        try:
            raw_context = incident_contexts_collection.find_one({"incident_id": incident_id}, {"_id": 0})
        except Exception:
            pass

    return {
        "incident_id": str(incident.id),
        "status": incident.status.value,
        "context_available": bool(raw_context),
        "stage": "PRE_DIAGNOSIS (Captured before AI Diagnosis)",
        "raw_context": raw_context or "Not yet captured or missing",
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 12: WebSocket Events
# ════════════════════════════════════════════════════════════

@app.websocket("/api/v1/events/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard events.
    """
    await ws_manager.connect(websocket)
    try:
        while True:
            # We don't expect messages from the client right now,
            # but we need to keep the connection open and detect disconnects.
            data = await websocket.receive_text()
            logger.debug(f"[WS] Received from client: {data}")
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"[WS] Connection error: {e}")
        await ws_manager.disconnect(websocket)
