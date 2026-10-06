import os
import hashlib
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

app = FastAPI(title="AutoFixOps API Gateway")

# CORS — allow dashboard direct access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    logger.info("Initializing relational database schema...")
    init_relational_db()
    
    # Start Redis WebSocket listener
    asyncio.create_task(redis_listener(ws_manager))
    
    logger.info("AutoFixOps API Gateway started successfully.")


# Statuses that end an incident for ingestion de-duplication purposes.
# HUMAN_APPROVED / HUMAN_REJECTED are inert legacy values (feature removed);
# rows already in them can never move again, so they must count as closed.
_CLOSED_STATUSES = [
    IncidentStatus.RESOLVED, IncidentStatus.FAILED,
    IncidentStatus.HUMAN_APPROVED, IncidentStatus.HUMAN_REJECTED,
]


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
            }
            for i in incidents
        ],
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 3: Incident Detail + Context
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/incidents/{incident_id}/context")
def get_incident_context(incident_id: str, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    # Fetch MongoDB context
    mongo_ctx = incident_contexts_collection.find_one(
        {"incident_id": incident_id}, {"_id": 0}
    )

    # Fetch remediation audits
    audits = (
        db.query(RemediationAudit)
        .filter(RemediationAudit.incident_id == incident_id)
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
def approve_escalation(incident_id: str, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    if incident.status != IncidentStatus.ESCALATED:
        raise HTTPException(
            status_code=409,
            detail=f"Incident is in state {incident.status.value}, not ESCALATED.",
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
        raise HTTPException(status_code=502, detail=f"Failed to reach target: {str(e)}")

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
