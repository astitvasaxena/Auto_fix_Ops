"""
Confidence-based routing and human approval for AI patch generation (Stage 2).
==============================================================================
Ties engine.routing_policy to the database. One PatchRoutingDecision row exists
per incident and diagnosis version (sha256 of the stored ai_report):

  AUTOMATIC_AI             decision AUTO      generation may start without approval
  HUMAN_APPROVAL_REQUIRED  decision PENDING   -> APPROVED (generation may start) | REJECTED (never)

Every value comes from the stored incident, never from a client: the API only
passes an incident id, an approve/reject verb and the actor information it
actually has (there is no authentication, so no identity is invented).

Generation is claimed atomically (generation_requested_at), so page refreshes,
repeated clicks, duplicate approvals and the post-diagnosis trigger start at
most one run per diagnosis version. The run itself is engine.patch_proposer's
run_for_incident, which re-reads the approval from the database and enforces
every safety check again. Nothing here applies, commits, pushes or deploys.
"""

import datetime
import os
import uuid
from typing import Callable, Optional

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from engine.ai_diagnostics import AIIncidentReport
from engine.patch_proposer import (
    FAILED, NOT_GENERATED, RETRYABLE_NOT_GENERATED, RUNNING, STALE_AFTER_S, VALIDATED, _error_summary, _one_line, _utcnow, effective_status,
    get_latest_proposal, read_system_mode, report_sha256, run_for_incident, serialise_proposal, settings_from_env,
)
from engine.routing_policy import (
    APPROVED, AUTO, AUTOMATIC_AI, HUMAN_APPROVAL_REQUIRED, PENDING, REJECTED, ROUTING_THRESHOLD,
    RoutingError, routing_mode, threshold_conflict, valid_confidence,
)

MAX_AUDIT_EVENTS = 50
STARTED = "STARTED"   # decision.proposal_status while the claiming job runs the proposer


def auto_generation_enabled(env=None) -> bool:
    """PATCH_PROPOSALS_AUTO (default true): may the automatic route start without an explicit request?"""
    env = os.environ if env is None else env
    return str(env.get("PATCH_PROPOSALS_AUTO", "true")).strip().lower() == "true"

# HTTP status for each RoutingError code (used by the API layer).
HTTP_STATUS = {
    "INCIDENT_NOT_FOUND": 404, "PROPOSAL_NOT_FOUND": 404,
    "INCIDENT_NOT_DIAGNOSED": 409, "NO_AI_REPORT": 409, "ALREADY_DECIDED": 409,
    "APPROVAL_NOT_APPLICABLE": 409, "SYSTEM_DISABLED": 409, "RETRY_NOT_ALLOWED": 409, "CONFIG_INVALID": 409,
    "APPROVAL_REQUIRED": 403, "HUMAN_REJECTED": 403,
    "REPORT_INVALID": 422, "CONFIDENCE_INVALID": 422, "CONFIDENCE_MISSING": 422,
    "DISPATCH_FAILED": 503, "DATABASE_ERROR": 503,
}


def _iso(value) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def _event(kind: str, now, **fields) -> dict:
    return {"event": kind, "at": _iso(now), **fields}


def _append(log, event: dict) -> list:
    return (list(log or []) + [event])[-MAX_AUDIT_EVENTS:]


def clean_actor(actor: Optional[dict]) -> dict:
    """Actor info as recorded. authenticated is True only if the caller says it verified an identity."""
    actor = actor if isinstance(actor, dict) else {}
    authenticated = actor.get("authenticated") is True and bool(actor.get("identity"))
    return {
        "authenticated": authenticated,
        "identity": _one_line(actor["identity"], 120) if authenticated else None,
        "source": _one_line(actor.get("source") or "unknown", 40),
        "client_host": _one_line(actor["client_host"], 64) if actor.get("client_host") else None,
        "user_agent": _one_line(actor["user_agent"], 160) if actor.get("user_agent") else None,
    }


# ─── Route computation (read-only) ───

def route_incident(incident) -> dict:
    """Route for an incident's stored report. Raises RoutingError when it cannot be routed."""
    status = incident.status.value if hasattr(incident.status, "value") else str(incident.status)
    if status != "DIAGNOSED":
        raise RoutingError("INCIDENT_NOT_DIAGNOSED", f"incident status is {status}")
    report = incident.ai_report
    if incident.diagnosed_by != "AI_ENGINE" or not isinstance(report, dict):
        raise RoutingError("NO_AI_REPORT", "the incident has no stored AI diagnosis report")
    if report.get("report_version") != 1 or report.get("role") != "ANALYST_ONLY":
        raise RoutingError("REPORT_INVALID", "unexpected report version or role")
    confidence = valid_confidence(report.get("confidence"))   # NaN/inf/bool/str/missing rejected first
    try:
        AIIncidentReport.model_validate({k: report.get(k) for k in AIIncidentReport.model_fields}, strict=True)
    except ValidationError as exc:
        fields = sorted({str(err.get("loc", ("?",))[0]) for err in exc.errors()})
        raise RoutingError("REPORT_INVALID", f"schema errors in {', '.join(fields)[:200]}")
    route = AUTOMATIC_AI if confidence >= ROUTING_THRESHOLD else HUMAN_APPROVAL_REQUIRED
    return {"route": route, "confidence": confidence, "report_sha256": report_sha256(report),
            "approval_required": route == HUMAN_APPROVAL_REQUIRED}


def config_problem(db, incident) -> Optional[str]:
    """Why generation could never run under the current configuration, or None.

    Checked before a route is recorded, so a conflicting threshold (or any invalid PATCH_* setting)
    is refused up front instead of routing automatically and then failing inside the proposer.
    """
    from api.models import ProjectConfig

    try:
        settings_from_env()
    except (TypeError, ValueError) as exc:
        return str(exc)
    if incident.project_id:
        project = db.query(ProjectConfig).filter(ProjectConfig.id == incident.project_id).first()
        if project is not None:
            return threshold_conflict(project.confidence_threshold, "project confidence_threshold")
    return None


def _load_incident(db, incident_uuid, lock=False):
    from api.models import Incident

    query = db.query(Incident).filter(Incident.id == incident_uuid)
    incident = (query.with_for_update() if lock else query).first()
    if incident is None:
        raise RoutingError("INCIDENT_NOT_FOUND", "incident not found")
    return incident


def _decision_row(db, incident_uuid, sha):
    from api.models import PatchRoutingDecision

    return (db.query(PatchRoutingDecision)
            .filter(PatchRoutingDecision.incident_id == incident_uuid, PatchRoutingDecision.report_sha256 == sha)
            .first())


def serialise_decision(row) -> dict:
    return {
        "decision_id": str(row.id), "incident_id": str(row.incident_id), "report_sha256": row.report_sha256,
        "diagnosis_confidence": row.diagnosis_confidence, "threshold": ROUTING_THRESHOLD,
        "route": row.route, "routing_mode": routing_mode(row.route, row.decision),
        "approval_required": bool(row.approval_required), "decision": row.decision,
        "decided_by": row.decided_by, "decision_reason": row.decision_reason, "decided_at": _iso(row.decided_at),
        "generation_requested_at": _iso(row.generation_requested_at),
        "proposal_id": str(row.proposal_id) if row.proposal_id else None,
        "proposal_status": row.proposal_status, "proposal_reason_code": row.proposal_reason_code,
        "audit_log": list(row.audit_log or []), "created_at": _iso(row.created_at), "recorded": True,
    }


# ─── Writes ───

def ensure_decision(session_factory, incident_uuid, now=None) -> dict:
    """Creates (or returns) the routing row for the incident's current report."""
    from api.models import PatchRoutingDecision

    now = now or _utcnow()
    db = session_factory()
    try:
        incident = _load_incident(db, incident_uuid, lock=True)
        info = route_incident(incident)
        problem = config_problem(db, incident)
        if problem:
            raise RoutingError("CONFIG_INVALID", problem)
        row = _decision_row(db, incident.id, info["report_sha256"])
        if row is None:
            row = PatchRoutingDecision(
                id=uuid.uuid4(), incident_id=incident.id, report_sha256=info["report_sha256"],
                diagnosis_confidence=info["confidence"], route=info["route"],
                approval_required=info["approval_required"],
                decision=AUTO if info["route"] == AUTOMATIC_AI else PENDING,
                audit_log=[_event("ROUTE_COMPUTED", now, confidence=info["confidence"], route=info["route"],
                                  threshold=ROUTING_THRESHOLD, approval_required=info["approval_required"])],
                created_at=now, updated_at=now)
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()   # a concurrent request created it first
                row = _decision_row(db, incident.id, info["report_sha256"])
                if row is None:
                    raise
        return serialise_decision(row)
    except RoutingError:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - reported without SQL text
        db.rollback()
        raise RoutingError("DATABASE_ERROR", _error_summary(exc))
    finally:
        db.close()


def decide(session_factory, incident_uuid, verdict: str, actor: Optional[dict], reason: Optional[str] = None,
           now=None) -> tuple:
    """Records APPROVED or REJECTED for a PENDING decision. Returns (decision, changed).

    Repeating the same verdict is a no-op (changed False); the opposite verdict is ALREADY_DECIDED.
    An approval is refused while the kill switch is DISABLED (a rejection is still recorded).
    """
    from api.models import PatchRoutingDecision

    if verdict not in (APPROVED, REJECTED):
        raise ValueError("verdict must be APPROVED or REJECTED")
    now = now or _utcnow()
    if verdict == APPROVED:
        try:
            mode = read_system_mode(session_factory)
        except Exception as exc:  # noqa: BLE001
            raise RoutingError("DATABASE_ERROR", f"kill switch could not be read: {_error_summary(exc)}")
        if mode == "DISABLED":
            raise RoutingError("SYSTEM_DISABLED", "the kill switch is DISABLED; approvals are not accepted")
    current = ensure_decision(session_factory, incident_uuid, now)
    if current["route"] != HUMAN_APPROVAL_REQUIRED:
        raise RoutingError("APPROVAL_NOT_APPLICABLE",
                           f"confidence {current['diagnosis_confidence']:.4f} >= {ROUTING_THRESHOLD:.2f}: "
                           "the automatic AI route needs no approval")
    who = clean_actor(actor)
    note = _one_line(reason, 300) if reason else None
    db = session_factory()
    try:
        row = db.query(PatchRoutingDecision).filter(PatchRoutingDecision.id == uuid.UUID(current["decision_id"])).first()
        event = _event(verdict, now, actor=who, reason=note)
        # Conditional update: only one PENDING -> APPROVED/REJECTED transition can ever succeed.
        changed = (db.query(PatchRoutingDecision)
                   .filter(PatchRoutingDecision.id == row.id, PatchRoutingDecision.decision == PENDING)
                   .update({"decision": verdict, "decided_by": who, "decision_reason": note, "decided_at": now,
                            "updated_at": now, "audit_log": _append(row.audit_log, event)},
                           synchronize_session=False))
        db.commit()
        db.expire_all()
        row = db.query(PatchRoutingDecision).filter(PatchRoutingDecision.id == row.id).first()
        if not changed and row.decision != verdict:
            raise RoutingError("ALREADY_DECIDED", f"this diagnosis was already {row.decision}")
        return serialise_decision(row), bool(changed)
    except RoutingError:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise RoutingError("DATABASE_ERROR", _error_summary(exc))
    finally:
        db.close()


def _authorise(decision: dict):
    if decision["route"] == AUTOMATIC_AI and decision["decision"] == AUTO:
        return
    if decision["route"] == HUMAN_APPROVAL_REQUIRED and decision["decision"] == APPROVED:
        return
    if decision["decision"] == REJECTED:
        raise RoutingError("HUMAN_REJECTED", "a human rejected AI patch generation for this diagnosis")
    raise RoutingError("APPROVAL_REQUIRED",
                       f"confidence {decision['diagnosis_confidence']:.4f} < {ROUTING_THRESHOLD:.2f}: "
                       "a human must approve AI patch generation first")


def _retry_allowed(db, decision: dict, now) -> bool:
    """May a claimed generation be requested again? Only after a FAILED run, a transient NOT_GENERATED
    (kill switch, configuration, approval state), or a claim that went stale with no live run.

    This only re-opens the claim: begin_run still refuses a second model decision for the version.
    """
    status, reason = decision["proposal_status"], decision["proposal_reason_code"]
    if status == FAILED or (status == NOT_GENERATED and reason in RETRYABLE_NOT_GENERATED):
        return True
    if status in (None, STARTED) and decision["generation_requested_at"]:
        requested = datetime.datetime.fromisoformat(decision["generation_requested_at"].rstrip("Z"))
        row, latest_status, _ = get_latest_proposal(db, uuid.UUID(decision["incident_id"]), now)
        active = row is not None and latest_status == RUNNING
        return not active and requested < now - datetime.timedelta(seconds=STALE_AFTER_S)
    return False


def request_generation(session_factory, incident_uuid, dispatch: Callable[[str, str, str], None], *,
                       retry: bool = False, trigger: str = "api", now=None) -> dict:
    """Starts at most one generation for the current diagnosis. Returns {"started", "decision"}.

    Refuses (RoutingError) when the kill switch is DISABLED (checked first, so it wins over every
    other reason), the configuration conflicts, approval is missing or a human rejected it. A second
    request while one is claimed is a no-op ("started": False). dispatch receives the claim token;
    run_generation only runs for the claim that is still current.
    """
    from api.models import PatchRoutingDecision

    now = now or _utcnow()
    try:
        mode = read_system_mode(session_factory)
    except Exception as exc:  # noqa: BLE001 - cannot show the switch is on: do not start
        raise RoutingError("DATABASE_ERROR", f"kill switch could not be read: {_error_summary(exc)}")
    if mode == "DISABLED":
        raise RoutingError("SYSTEM_DISABLED", "the kill switch is DISABLED")
    decision = ensure_decision(session_factory, incident_uuid, now)
    _authorise(decision)

    decision_id = uuid.UUID(decision["decision_id"])
    db = session_factory()
    try:
        row = db.query(PatchRoutingDecision).filter(PatchRoutingDecision.id == decision_id).first()
        claim = db.query(PatchRoutingDecision).filter(PatchRoutingDecision.id == decision_id)
        if row.generation_requested_at is None:
            claim = claim.filter(PatchRoutingDecision.generation_requested_at.is_(None))
        elif retry and _retry_allowed(db, decision, now):
            claim = claim.filter(PatchRoutingDecision.generation_requested_at == row.generation_requested_at)
        elif retry:
            raise RoutingError("RETRY_NOT_ALLOWED", "only a FAILED or abandoned generation can be retried")
        else:
            return {"started": False, "decision": decision}
        event = _event("GENERATION_REQUESTED", now, trigger=_one_line(trigger, 20), retry=bool(retry),
                       routing_mode=decision["routing_mode"])
        claimed = claim.update({"generation_requested_at": now, "proposal_id": None, "proposal_status": None,
                                "proposal_reason_code": None, "updated_at": now,
                                "audit_log": _append(row.audit_log, event)}, synchronize_session=False)
        db.commit()
    except RoutingError:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise RoutingError("DATABASE_ERROR", _error_summary(exc))
    finally:
        db.close()
    if not claimed:   # another request claimed it between our read and the conditional update
        return {"started": False, "decision": ensure_decision(session_factory, incident_uuid, now)}
    try:
        dispatch(str(incident_uuid), str(decision_id), _iso(now))   # the claim token
    except Exception as exc:  # noqa: BLE001 - e.g. the Celery broker is down
        _release_claim(session_factory, decision_id, now, f"dispatch failed: {_error_summary(exc)}")
        raise RoutingError("DISPATCH_FAILED", f"the generation job could not be queued ({_error_summary(exc)})")
    return {"started": True, "decision": ensure_decision(session_factory, incident_uuid, now)}


def _release_claim(session_factory, decision_id, claimed_at, why: str):
    from api.models import PatchRoutingDecision

    db = session_factory()
    try:
        row = db.query(PatchRoutingDecision).filter(PatchRoutingDecision.id == decision_id).first()
        if row is not None and row.generation_requested_at == claimed_at:
            row.generation_requested_at = None
            row.audit_log = _append(row.audit_log, _event("GENERATION_RELEASED", _utcnow(), reason=_one_line(why, 200)))
            row.updated_at = _utcnow()
            db.commit()
    except Exception:  # noqa: BLE001 - best effort; a stale claim can be retried later
        db.rollback()
    finally:
        db.close()


def run_generation(session_factory, incident_uuid, decision_id, claim, repo_root, *, runner=None,
                   now_fn=_utcnow, **runner_kw) -> dict:
    """Worker side: re-checks the decision, runs the proposer and links the outcome to the decision.

    claim is the token request_generation handed to dispatch. A job runs only while its claim is
    still the decision's current one and has no outcome yet, so a job superseded by a retry, a
    replayed or redelivered job, or a forged job naming a finished decision does nothing.
    """
    from api.models import PatchRoutingDecision

    runner = runner or run_for_incident
    decision_uuid = uuid.UUID(str(decision_id))
    db = session_factory()
    try:
        row = db.query(PatchRoutingDecision).filter(PatchRoutingDecision.id == decision_uuid).first()
        if row is None or str(row.incident_id) != str(incident_uuid):
            return {"started": False, "reason_code": "DECISION_MISSING"}
        decision = serialise_decision(row)
        claimed_at = row.generation_requested_at
        skip = None
        if claimed_at is None or not isinstance(claim, str) or _iso(claimed_at) != claim:
            skip = "STALE_CLAIM"
        elif row.proposal_status == STARTED:
            skip = "ALREADY_STARTED"
        elif row.proposal_status is not None:
            skip = "ALREADY_COMPLETED"
        if skip is None:
            try:
                _authorise(decision)
            except RoutingError as exc:
                skip = exc.code
        if skip is None:
            incident = _load_incident(db, row.incident_id)
            current = report_sha256(incident.ai_report) if isinstance(incident.ai_report, dict) else None
            if current != row.report_sha256:
                skip = "DIAGNOSIS_SUPERSEDED"
        if skip is None:
            # Atomically take the claim for this job: a second job with the same token (replayed,
            # redelivered or forged) loses here and never reaches the runner.
            taken = (db.query(PatchRoutingDecision)
                     .filter(PatchRoutingDecision.id == decision_uuid,
                             PatchRoutingDecision.generation_requested_at == claimed_at,
                             PatchRoutingDecision.proposal_status.is_(None))
                     .update({"proposal_status": STARTED, "updated_at": now_fn()}, synchronize_session=False))
            if not taken:
                skip = "ALREADY_STARTED"
                db.rollback()
                db.expire_all()
                row = db.query(PatchRoutingDecision).filter(PatchRoutingDecision.id == decision_uuid).first()
        if skip:
            row.audit_log = _append(row.audit_log, _event("GENERATION_SKIPPED", now_fn(), reason_code=skip))
            db.commit()
            return {"started": False, "reason_code": skip}
        db.commit()
    finally:
        db.close()

    try:
        result = runner(session_factory, uuid.UUID(str(incident_uuid)), repo_root, **runner_kw)
    except Exception as exc:  # noqa: BLE001 - recorded on the decision, never as success
        result = {"status": FAILED, "reason_code": "INTERNAL_ERROR", "proposal_id": None,
                  "reason_detail": _error_summary(exc)}
    busy = result.get("reason_code") == "BUSY"
    db = session_factory()
    try:
        row = db.query(PatchRoutingDecision).filter(PatchRoutingDecision.id == decision_uuid).first()
        # Only the job that took the claim records an outcome (or releases it after BUSY, when another
        # run - e.g. the CLI - holds the incident; begin_run still allows one model decision per version).
        if row is not None and row.generation_requested_at == claimed_at and row.proposal_status == STARTED:
            proposal_id = result.get("proposal_id")
            row.proposal_id = None if busy or not proposal_id else uuid.UUID(str(proposal_id))
            row.proposal_status = None if busy else result.get("status")
            row.proposal_reason_code = result.get("reason_code")
            if busy:   # a concurrent CLI run holds the incident; let a later request start again
                row.generation_requested_at = None
            row.audit_log = _append(row.audit_log, _event(
                "GENERATION_FINISHED", now_fn(), proposal_id=str(proposal_id) if proposal_id else None,
                status=result.get("status"), reason_code=result.get("reason_code"),
                persisted=bool(result.get("persisted"))))
            row.updated_at = now_fn()
            db.commit()
    finally:
        db.close()
    return {"started": True, "status": result.get("status"), "reason_code": result.get("reason_code"),
            "proposal_id": str(result["proposal_id"]) if result.get("proposal_id") else None}


# ─── Read-only view for the API / dashboard ───

def incident_view(db, incident_uuid, now=None) -> dict:
    """Routing + latest proposal for an incident. Never writes."""
    from api.models import PatchProposal

    incident = _load_incident(db, incident_uuid)
    routing, routing_error, requested_at = None, None, None
    try:
        info = route_incident(incident)
        problem = config_problem(db, incident)
        if problem:
            raise RoutingError("CONFIG_INVALID", problem)
    except RoutingError as exc:
        info, routing_error = None, {"code": exc.code, "detail": exc.detail}
    if info is not None:
        decision_row = _decision_row(db, incident.id, info["report_sha256"])
        if decision_row is not None:
            routing = serialise_decision(decision_row)
            requested_at = decision_row.generation_requested_at
        else:   # not yet recorded: the same computation, flagged as such
            decision = AUTO if info["route"] == AUTOMATIC_AI else PENDING
            routing = {"decision_id": None, "incident_id": str(incident.id), "report_sha256": info["report_sha256"],
                       "diagnosis_confidence": info["confidence"], "threshold": ROUTING_THRESHOLD,
                       "route": info["route"], "routing_mode": routing_mode(info["route"], decision),
                       "approval_required": info["approval_required"], "decision": decision, "decided_by": None,
                       "decision_reason": None, "decided_at": None, "generation_requested_at": None,
                       "proposal_id": None, "proposal_status": None, "proposal_reason_code": None,
                       "audit_log": [], "created_at": None, "recorded": False}
    row, status, reason = get_latest_proposal(db, incident.id, now)
    latest = serialise_proposal(row, status, reason) if row is not None else None
    if latest is not None:
        built_from = (latest.get("source_report") or {}).get("sha256")
        latest["matches_current_diagnosis"] = bool(info) and built_from == info["report_sha256"]
    history = (db.query(PatchProposal).filter(PatchProposal.incident_id == incident.id)
               .order_by(PatchProposal.created_at.desc()).limit(10).all())
    if routing is not None:
        running_since_claim = (row is not None and status == RUNNING and requested_at is not None
                               and row.created_at >= requested_at)
        routing["generation_state"] = _generation_state(routing, running_since_claim)
        # The backend decides whether a retry would be accepted; the dashboard only shows the button.
        try:
            _authorise(routing)
            authorised = True
        except RoutingError:
            authorised = False
        routing["retry_allowed"] = bool(routing["recorded"] and authorised
                                        and routing["generation_requested_at"]
                                        and _retry_allowed(db, routing, now or _utcnow()))
    return {
        "incident_id": str(incident.id),
        "incident_status": incident.status.value if hasattr(incident.status, "value") else str(incident.status),
        "routing": routing, "routing_error": routing_error, "latest_proposal": latest,
        "proposals": [{"id": str(p.id), "status": p.status, "reason_code": p.reason_code,
                       "created_at": _iso(p.created_at), "completed_at": _iso(p.completed_at)} for p in history],
        "stage": "PROPOSAL_ONLY", "applied": False, "deployment_available": False,
        "auto_generation_enabled": auto_generation_enabled(),
    }


def _generation_state(routing: dict, running_since_claim: bool) -> str:
    """NOT_REQUESTED | AWAITING_APPROVAL | REJECTED_BY_HUMAN | QUEUED | RUNNING | COMPLETED."""
    if routing["decision"] == REJECTED:
        return "REJECTED_BY_HUMAN"
    if routing["decision"] == PENDING:
        return "AWAITING_APPROVAL"
    if not routing["generation_requested_at"]:
        return "NOT_REQUESTED"
    if routing["proposal_status"] == STARTED or (not routing["proposal_status"] and running_since_claim):
        return "RUNNING"
    return "COMPLETED" if routing["proposal_status"] else "QUEUED"


def proposal_detail(db, proposal_id, now=None) -> dict:
    """One proposal with its effective (integrity- and staleness-checked) status."""
    from api.models import PatchProposal

    row = db.query(PatchProposal).filter(PatchProposal.id == proposal_id).first()
    if row is None:
        raise RoutingError("PROPOSAL_NOT_FOUND", "proposal not found")
    return serialise_proposal(row, *effective_status(row, now))


__all__ = ["route_incident", "ensure_decision", "decide", "request_generation", "run_generation",
           "incident_view", "proposal_detail", "clean_actor", "HTTP_STATUS", "RoutingError", "VALIDATED"]
