"""
Stage 2 API: AI patch proposals with confidence-based routing.

Thin HTTP layer over engine.patch_routing (routing/approval) and
engine.patch_proposer (generation + validation, run by the Celery worker).
Nothing here generates, applies, commits, pushes or deploys a patch.

Clients never send a confidence or a route: POST bodies must be JSON (415
otherwise, which blocks cross-site form/no-cors requests), accept only the
fields declared below (anything else is a 422), and the route is computed
from the stored diagnosis report. There is no authentication in this API, so
approvals record the client information that is actually available and are
marked authenticated=false.

LOCAL DEMONSTRATION ONLY: anyone who can reach this API can approve or reject.
Do not expose it publicly until authentication and authorization are added;
approve/reject should then require an authenticated user with an approver role.
"""

import uuid
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.database import SessionLocal, logger
from engine import patch_routing as routing
from engine.routing_policy import APPROVED, REJECTED, RoutingError

router = APIRouter(prefix="/api/v1", tags=["patch-proposals"])


# ─── Dependencies (overridable in tests) ───

def get_session_factory():
    return SessionLocal


def _celery_dispatch(incident_id: str, decision_id: str, claim: str):
    from workers.tasks import generate_patch_proposal

    generate_patch_proposal.apply_async(args=[incident_id, decision_id, claim])


def get_dispatcher() -> Callable[[str, str, str], None]:
    return _celery_dispatch


def require_json(request: Request):
    """POSTs must be application/json. A cross-site page can only send 'simple' content types
    without a CORS preflight, and the preflight is refused for origins outside the allowlist,
    so this stops cross-site form/no-cors requests from recording approvals or starting runs."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="UNSUPPORTED_MEDIA_TYPE: send a JSON body (Content-Type: application/json)")


# ─── Request / response models ───

class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry: bool = False


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(default=None, max_length=500)


class RoutingOut(BaseModel):
    decision_id: Optional[str]
    incident_id: str
    report_sha256: str
    diagnosis_confidence: float
    threshold: float
    route: str
    routing_mode: str
    approval_required: bool
    decision: str
    decided_by: Optional[Dict[str, Any]]
    decision_reason: Optional[str]
    decided_at: Optional[str]
    generation_requested_at: Optional[str]
    generation_state: str
    retry_allowed: bool = False
    proposal_id: Optional[str]
    proposal_status: Optional[str]
    proposal_reason_code: Optional[str]
    audit_log: List[Dict[str, Any]]
    created_at: Optional[str]
    recorded: bool


class ProposalOut(BaseModel):
    id: str
    incident_id: str
    created_at: Optional[str]
    completed_at: Optional[str]
    proposer_version: Optional[int]
    status: str
    effective_status: str
    reason_code: Optional[str]
    effective_reason_code: Optional[str]
    reason_detail: Optional[str]
    action_type: Optional[str]
    target_file: Optional[str]
    target_ref: Optional[Dict[str, Any]]
    base: Optional[Dict[str, Any]]
    patched_git_blob_sha: Optional[str]
    previous_values: Optional[Dict[str, Any]]
    proposed_values: Optional[Dict[str, Any]]
    candidates: Optional[Dict[str, Any]]
    unified_diff: Optional[str]
    diff_sha256: Optional[str]
    evidence: Optional[Dict[str, Any]]
    llm_decision: Optional[Dict[str, Any]]
    rationale: Optional[str]
    review_notes: Optional[List[str]]
    validation: Optional[Dict[str, Any]]
    model_info: Optional[Dict[str, Any]]
    source_report: Optional[Dict[str, Any]]
    matches_current_diagnosis: Optional[bool] = None
    applied: bool
    requires_human_approval: bool
    stage: str


class ProposalSummary(BaseModel):
    id: str
    status: str
    reason_code: Optional[str]
    created_at: Optional[str]
    completed_at: Optional[str]


class RoutingErrorOut(BaseModel):
    code: str
    detail: str


class IncidentProposalsOut(BaseModel):
    incident_id: str
    incident_status: str
    routing: Optional[RoutingOut]
    routing_error: Optional[RoutingErrorOut]
    latest_proposal: Optional[ProposalOut]
    proposals: List[ProposalSummary]
    stage: str
    applied: bool
    deployment_available: bool
    auto_generation_enabled: bool


class ActionOut(BaseModel):
    started: bool
    changed: Optional[bool] = None
    message: str
    view: IncidentProposalsOut


class DiffOut(BaseModel):
    proposal_id: str
    effective_status: str
    available: bool
    unified_diff: Optional[str]
    diff_sha256: Optional[str]
    target_file: Optional[str]
    applied: bool


class ValidationOut(BaseModel):
    proposal_id: str
    effective_status: str
    effective_reason_code: Optional[str]
    reason_detail: Optional[str]
    steps: List[Dict[str, Any]]
    rejected_candidate: Optional[Dict[str, Any]]


# ─── Helpers ───

def _http(exc: RoutingError) -> HTTPException:
    return HTTPException(status_code=routing.HTTP_STATUS.get(exc.code, 409), detail=f"{exc.code}: {exc.detail}")


def _actor(request: Request) -> dict:
    return {"authenticated": False, "identity": None, "source": "dashboard-api",
            "client_host": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent")}


def _view(session_factory, incident_id: uuid.UUID) -> dict:
    db = session_factory()
    try:
        return routing.incident_view(db, incident_id)
    finally:
        db.close()


# ─── Endpoints ───

@router.get("/incidents/{incident_id}/patch-proposals", response_model=IncidentProposalsOut)
def get_incident_patch_proposals(incident_id: uuid.UUID, session_factory=Depends(get_session_factory)):
    """Routing decision and latest proposal for an incident (read-only)."""
    try:
        return _view(session_factory, incident_id)
    except RoutingError as exc:
        raise _http(exc)


@router.post("/incidents/{incident_id}/patch-proposals", response_model=ActionOut, status_code=202,
             dependencies=[Depends(require_json)])
def start_patch_proposal(incident_id: uuid.UUID, body: Optional[GenerationRequest] = Body(default=None),
                         session_factory=Depends(get_session_factory), dispatch=Depends(get_dispatcher)):
    """Starts generation if the backend's route allows it (automatic, or approved). Idempotent."""
    body = body or GenerationRequest()
    try:
        result = routing.request_generation(session_factory, incident_id, dispatch, retry=body.retry, trigger="api")
    except RoutingError as exc:
        raise _http(exc)
    logger.info(f"[TRACE:{incident_id}] patch proposal requested (started={result['started']}, "
                f"route={result['decision']['routing_mode']})")
    message = "generation queued" if result["started"] else "generation already requested for this diagnosis"
    return {"started": result["started"], "message": message, "view": _view(session_factory, incident_id)}


@router.post("/incidents/{incident_id}/patch-proposals/approve", response_model=ActionOut,
             dependencies=[Depends(require_json)])
def approve_patch_generation(incident_id: uuid.UUID, request: Request,
                             body: Optional[DecisionRequest] = Body(default=None),
                             session_factory=Depends(get_session_factory), dispatch=Depends(get_dispatcher)):
    """Records a human approval (low-confidence route only), then starts generation once."""
    body = body or DecisionRequest()
    try:
        decision, changed = routing.decide(session_factory, incident_id, APPROVED, _actor(request), body.reason)
    except RoutingError as exc:
        raise _http(exc)
    logger.info(f"[TRACE:{incident_id}] AI patch generation APPROVED (changed={changed}, "
                f"decision={decision['decision_id']}, authenticated=False)")
    try:
        result = routing.request_generation(session_factory, incident_id, dispatch, trigger="approval")
    except RoutingError as exc:
        error = _http(exc)
        error.detail = f"approval recorded, but generation did not start: {error.detail}"
        raise error
    message = "approval recorded; generation queued" if result["started"] else "approval already recorded"
    return {"started": result["started"], "changed": changed, "message": message,
            "view": _view(session_factory, incident_id)}


@router.post("/incidents/{incident_id}/patch-proposals/reject", response_model=ActionOut,
             dependencies=[Depends(require_json)])
def reject_patch_generation(incident_id: uuid.UUID, request: Request,
                            body: Optional[DecisionRequest] = Body(default=None),
                            session_factory=Depends(get_session_factory)):
    """Records a human rejection: no proposal is ever generated for this diagnosis."""
    body = body or DecisionRequest()
    try:
        decision, changed = routing.decide(session_factory, incident_id, REJECTED, _actor(request), body.reason)
    except RoutingError as exc:
        raise _http(exc)
    logger.info(f"[TRACE:{incident_id}] AI patch generation REJECTED (changed={changed}, "
                f"decision={decision['decision_id']}, authenticated=False)")
    return {"started": False, "changed": changed, "message": "rejection recorded; no patch will be generated",
            "view": _view(session_factory, incident_id)}


def _proposal(session_factory, proposal_id: uuid.UUID) -> dict:
    db = session_factory()
    try:
        return routing.proposal_detail(db, proposal_id)
    except RoutingError as exc:
        raise _http(exc)
    finally:
        db.close()


@router.get("/patch-proposals/{proposal_id}", response_model=ProposalOut)
def get_patch_proposal(proposal_id: uuid.UUID, session_factory=Depends(get_session_factory)):
    return _proposal(session_factory, proposal_id)


@router.get("/patch-proposals/{proposal_id}/diff", response_model=DiffOut)
def get_patch_proposal_diff(proposal_id: uuid.UUID, session_factory=Depends(get_session_factory)):
    """The stored diff, only for an intact VALIDATED proposal (otherwise unified_diff is null)."""
    data = _proposal(session_factory, proposal_id)
    return {"proposal_id": data["id"], "effective_status": data["effective_status"],
            "available": data["unified_diff"] is not None, "unified_diff": data["unified_diff"],
            "diff_sha256": data["diff_sha256"], "target_file": data["target_file"], "applied": False}


@router.get("/patch-proposals/{proposal_id}/validation", response_model=ValidationOut)
def get_patch_proposal_validation(proposal_id: uuid.UUID, session_factory=Depends(get_session_factory)):
    data = _proposal(session_factory, proposal_id)
    validation = data.get("validation") or {}
    return {"proposal_id": data["id"], "effective_status": data["effective_status"],
            "effective_reason_code": data["effective_reason_code"], "reason_detail": data["reason_detail"],
            "steps": validation.get("steps") or [], "rejected_candidate": validation.get("rejected_candidate")}
