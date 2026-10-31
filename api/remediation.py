"""
Stages 3-4 API: human approval, GitHub PR, controlled deployment and recovery verification.

Who may decide (approve / reject / close / advance):
  - APPROVER_TOKENS="name:token,..." set  -> a request must send
    "Authorization: Bearer <token>"; the matching name is recorded as the
    authenticated approver identity.
  - not set (local demonstration)         -> only requests from this machine
    (loopback) are accepted, and they are recorded as authenticated=false with
    no identity. Remote clients get 403.
POSTs must be JSON (415 otherwise) so a cross-site page cannot submit them.
The browser never sends a confidence, a value or a target: the proposal id it
approves is checked against the latest validated proposal on the server.
"""

import hmac
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.database import SessionLocal, logger
from api.patch_proposals import require_json
from engine import remediation_workflow as wf

router = APIRouter(prefix="/api/v1", tags=["remediation"])

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
LOCAL_ORIGINS = {"http://localhost:3000", "http://127.0.0.1:3000"}   # the dashboard (same as the CORS allowlist)


def get_session_factory():
    return SessionLocal


def _celery_dispatch(run_id: str):
    from workers.tasks import advance_remediation

    advance_remediation.apply_async(args=[run_id])


def get_dispatcher():
    return _celery_dispatch


def _approver_tokens() -> dict:
    tokens = {}
    for item in (os.getenv("APPROVER_TOKENS") or "").split(","):
        name, _, token = item.strip().partition(":")
        if name and len(token) >= 16:
            tokens[token] = name.strip()
    return tokens


def require_approver(request: Request) -> dict:
    """The verified approver, or 401/403. Never invents an identity."""
    tokens = _approver_tokens()
    host = request.client.host if request.client else None
    actor = {"client_host": host, "user_agent": request.headers.get("user-agent")}
    if tokens:
        header = request.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        for token, name in tokens.items():
            if supplied and hmac.compare_digest(supplied.encode(), token.encode()):
                return {**actor, "authenticated": True, "identity": name, "source": "approver-token"}
        raise HTTPException(status_code=401, detail="APPROVER_AUTH_REQUIRED: send Authorization: Bearer <approver token>")
    if host not in LOOPBACK:
        raise HTTPException(status_code=403, detail="LOCAL_ONLY: remote approvals require APPROVER_TOKENS to be configured")
    # A loopback source address is not enough: a DNS-rebinding page in the operator's browser also
    # arrives from 127.0.0.1. Require a local Host header and, if sent, a dashboard Origin.
    hostname = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    origin = request.headers.get("origin")
    if hostname not in LOOPBACK or (origin and origin not in LOCAL_ORIGINS):
        raise HTTPException(status_code=403, detail="LOCAL_ONLY: request must target localhost from the local dashboard")
    return {**actor, "authenticated": False, "identity": None, "source": "local-dashboard"}


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: uuid.UUID
    reason: Optional[str] = Field(default=None, max_length=500)


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(default=None, max_length=500)


def _http(exc: wf.WorkflowError) -> HTTPException:
    return HTTPException(status_code=wf.HTTP_STATUS.get(exc.code, 409), detail=f"{exc.code}: {exc.detail}")


def _view(session_factory, incident_id):
    db = session_factory()
    try:
        return wf.remediation_view(db, incident_id)
    except wf.WorkflowError as exc:
        raise _http(exc)
    finally:
        db.close()


@router.get("/incidents/{incident_id}/remediation")
def get_remediation(incident_id: uuid.UUID, session_factory=Depends(get_session_factory)):
    """Approval state, PR, deployment, verification and the incident timeline (read-only)."""
    view = _view(session_factory, incident_id)
    view["approver_auth"] = "token" if _approver_tokens() else "local-only"
    return view


@router.post("/incidents/{incident_id}/remediation/approve", dependencies=[Depends(require_json)])
def approve_remediation(incident_id: uuid.UUID, body: DecisionIn, actor=Depends(require_approver),
                        session_factory=Depends(get_session_factory), dispatch=Depends(get_dispatcher)):
    try:
        run, changed = wf.decide(session_factory, incident_id, body.proposal_id, wf.APPROVED, actor, body.reason)
    except wf.WorkflowError as exc:
        raise _http(exc)
    dispatched = False
    if changed:
        try:
            dispatch(run["id"])
            dispatched = True
        except Exception as exc:   # the run stays APPROVED; "advance" retries the step
            logger.error(f"[TRACE:{incident_id}] remediation dispatch failed: {exc.__class__.__name__}")
    return {"changed": changed, "dispatched": dispatched, "run": run, "view": _view(session_factory, incident_id)}


@router.post("/incidents/{incident_id}/remediation/reject", dependencies=[Depends(require_json)])
def reject_remediation(incident_id: uuid.UUID, body: DecisionIn, actor=Depends(require_approver),
                       session_factory=Depends(get_session_factory)):
    try:
        run, changed = wf.decide(session_factory, incident_id, body.proposal_id, wf.REJECTED, actor, body.reason)
    except wf.WorkflowError as exc:
        raise _http(exc)
    return {"changed": changed, "run": run, "view": _view(session_factory, incident_id)}


@router.post("/incidents/{incident_id}/remediation/advance", dependencies=[Depends(require_json)])
def advance_remediation(incident_id: uuid.UUID, actor=Depends(require_approver),
                        session_factory=Depends(get_session_factory), dispatch=Depends(get_dispatcher)):
    """Re-runs the current step (e.g. after configuring GitHub or deployment, or to re-check a PR)."""
    view = _view(session_factory, incident_id)
    run = view.get("run")
    if not run or run["final"]:
        raise HTTPException(status_code=409, detail="NOTHING_TO_ADVANCE: no active remediation run")
    try:
        dispatch(run["id"])
    except Exception:
        raise HTTPException(status_code=503, detail="DISPATCH_FAILED: the worker queue is unreachable")
    return {"dispatched": True, "view": view}


@router.post("/incidents/{incident_id}/close", dependencies=[Depends(require_json)])
def close_incident(incident_id: uuid.UUID, body: ReasonIn, actor=Depends(require_approver),
                   session_factory=Depends(get_session_factory)):
    """A human closes the incident without automated remediation (recorded on the timeline)."""
    try:
        result = wf.close_incident(session_factory, incident_id, actor, body.reason)
    except wf.WorkflowError as exc:
        raise _http(exc)
    return {**result, "view": _view(session_factory, incident_id)}


# ─── Kubernetes target status and crash-loop chaos (local demo) ───

class K8sChaosIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault: str = Field(pattern=r"^(crash_loop|memory_leak|cpu_spike|reset)$")
    confirmation: str


def _k8s_target():
    from engine import deployer as dep
    from engine.telemetry import DEFAULT_WORKLOAD

    cfg = dep.read_config()
    workload = os.getenv("TARGET_WORKLOAD", DEFAULT_WORKLOAD)
    namespace = os.getenv("TARGET_NAMESPACE", "autofixops")
    if f"{namespace}/{workload}" not in cfg.allowed_targets:
        raise dep.DeployBlocked("TARGET_NOT_ALLOWLISTED", f"{namespace}/{workload} is not in DEPLOY_ALLOWED_TARGETS")
    return cfg, dep.Target(namespace, workload, "target-app", "memory", "0", "0")


@router.get("/target/kubernetes")
def kubernetes_target_status():
    """Live pod health of the Kubernetes target (read-only kubectl) and the chaos switch."""
    from engine import deployer as dep
    from engine import k8s_chaos, k8s_evidence

    evidence = k8s_evidence.collect({"namespace": os.getenv("TARGET_NAMESPACE", "autofixops")})
    chaos = None
    if evidence.get("available"):
        try:
            cfg, target = _k8s_target()
            chaos = k8s_chaos.status(cfg, target)
        except dep.DeployBlocked as exc:
            chaos = {"error": f"{exc.code}: {exc.detail}"}
    return {"evidence": evidence, "chaos": chaos,
            "chaos_enabled": os.getenv("CHAOS_K8S_ENABLED", "false").strip().lower() == "true"}


@router.post("/chaos/kubernetes", dependencies=[Depends(require_json)])
def kubernetes_chaos(body: K8sChaosIn, actor=Depends(require_approver)):
    """Chaos on the allowlisted Kubernetes target (crash_loop, memory_leak, cpu_spike or reset).
    Opt-in (CHAOS_K8S_ENABLED=true); every fault expires after CHAOS_TTL_MINUTES.
    It never creates an incident: detection must come from Prometheus/Alertmanager."""
    from api.config_helpers import is_system_disabled
    from engine import deployer as dep
    from engine import k8s_chaos

    if os.getenv("CHAOS_K8S_ENABLED", "false").strip().lower() != "true":
        raise HTTPException(status_code=403, detail="CHAOS_DISABLED: set CHAOS_K8S_ENABLED=true to allow Kubernetes chaos")
    if body.confirmation != "CONFIRM":
        raise HTTPException(status_code=400, detail="Must type CONFIRM to inject chaos.")
    if is_system_disabled():
        raise HTTPException(status_code=503, detail="System is DISABLED. Chaos injection blocked.")
    try:
        cfg, target = _k8s_target()
        if body.fault == "reset":
            result = k8s_chaos.reset(cfg, target)
        else:
            result = k8s_chaos.inject(cfg, target, body.fault)
    except dep.DeployBlocked as exc:
        raise HTTPException(status_code=409, detail=f"{exc.code}: {exc.detail}")
    logger.info(f"[CHAOS] Kubernetes {body.fault}: {result} (by {actor.get('identity') or actor.get('client_host')})")
    return {**result, "incident_created": False,
            "note": "No incident is created here; Prometheus/Alertmanager must detect the failure."}
