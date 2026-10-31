"""
Stages 3-4: approval -> GitHub pull request -> controlled deployment -> recovery verification.
=============================================================================================
Starts from a VALIDATED Stage 2 proposal (an exact, sandbox-checked diff of one
resource limit) and takes it, step by step, to a verified recovery:

  approve (human) -> RemediationRun APPROVED            incident DIAGNOSED -> POLICY_APPROVED
  publish PR       -> PR_OPEN (or READY_TO_DEPLOY        POLICY_APPROVED -> REMEDIATING
                      when REMEDIATION_REQUIRE_PR=false)  -> PENDING_PR_MERGE
  PR merged        -> READY_TO_DEPLOY (merged file must equal the approved patch)
  deploy           -> DEPLOYING -> VERIFYING
  verify rounds    -> RESOLVED                           -> VERIFIED -> RESOLVED
               or  -> ROLLED_BACK / ROLLBACK_FAILED      -> FAILED

Rules that keep it safe:
  - Every remediation needs a human approval, whatever the diagnosis confidence.
    The approval names the exact proposal and is checked against the latest
    proposal's integrity, the current diagnosis version and the kill switch.
  - The PR content is rebuilt from the stored diff and must hash to the stored
    patched blob; the merged file must hash to it too before anything is deployed.
  - One step runs at a time per run (claim token) and one deployment per target
    (database lock). Steps that cannot run yet are recorded as *blocked* and
    change nothing; they can be retried.
  - The deployment applies only the approved value (engine.deployer), and
    verification failure rolls it back.
Nothing in this module calls an LLM.
"""

import datetime
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError

from engine import deployer as dep
from engine import recovery_verifier as rv
from engine.patch_diff import PatchDiffError, apply_unified_diff_strict, parse_unified_diff_strict
from engine.patch_proposer import VALIDATED, get_latest_proposal, report_sha256
from engine.patch_routing import clean_actor
from engine.patch_safety import git_blob_sha

logger = logging.getLogger("autofixops")

APPROVED, REJECTED, PR_OPEN, READY_TO_DEPLOY = "APPROVED", "REJECTED", "PR_OPEN", "READY_TO_DEPLOY"
DEPLOYING, VERIFYING, RESOLVED = "DEPLOYING", "VERIFYING", "RESOLVED"
FAILED, ROLLED_BACK, ROLLBACK_FAILED = "FAILED", "ROLLED_BACK", "ROLLBACK_FAILED"
FINAL_STAGES = {REJECTED, RESOLVED, FAILED, ROLLED_BACK, ROLLBACK_FAILED}
STEP_STALE_S = 900          # minimum age before a claimed step is assumed dead and may be re-claimed


def step_stale_s(env) -> int:
    """A claim may be taken over only after the longest possible step (deploy + failed rollout
    + rollback rollout) has certainly ended, so two workers never run the same step."""
    try:
        rollout = int(env.get("DEPLOY_ROLLOUT_TIMEOUT_SECONDS", 120))
    except (TypeError, ValueError):
        rollout = 120
    return max(STEP_STALE_S, 2 * (rollout + 15) + 300)
MAX_ROUNDS_KEPT = 40
MAX_AUDIT_EVENTS = 200

HTTP_STATUS = {
    "INCIDENT_NOT_FOUND": 404, "RUN_NOT_FOUND": 404, "SYSTEM_DISABLED": 423, "PROPOSAL_NOT_LATEST": 409,
    "PROPOSAL_NOT_VALIDATED": 409, "DIAGNOSIS_CHANGED": 409, "INCIDENT_NOT_DIAGNOSED": 409,
    "ALREADY_DECIDED": 409, "RUN_ACTIVE": 409, "REASON_REQUIRED": 422, "TARGET_INVALID": 409,
    "RESTRICTED_NAMESPACE": 403, "NOT_CLOSABLE": 409,
}


class WorkflowError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _utcnow():
    return datetime.datetime.utcnow()


def _iso(value) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def _event(kind: str, now, **fields) -> dict:
    return {"event": kind, "at": _iso(now), **fields}


def _append(log, *events) -> list:
    return (list(log or []) + list(events))[-MAX_AUDIT_EVENTS:]


def _flag(env, name, default) -> bool:
    return str(env.get(name, default)).strip().lower() in ("1", "true", "yes")


def _system_mode(db) -> str:
    from api.models import SystemConfig

    row = db.query(SystemConfig).filter(SystemConfig.id == "global").first()
    mode = getattr(row, "system_mode", None)
    return getattr(mode, "value", mode) or "ACTIVE"


_CHAIN = ["DIAGNOSED", "POLICY_APPROVED", "REMEDIATING", "PENDING_PR_MERGE", "VERIFIED", "RESOLVED"]


def _move(incident, *statuses):
    """Applies each incident transition in order, skipping ones already reached (so a replayed
    step never moves the incident backwards)."""
    from api.models import IncidentStatus

    for name in statuses:
        current = incident.status.value
        if current == name or (current in _CHAIN and name in _CHAIN and _CHAIN.index(current) > _CHAIN.index(name)):
            continue
        incident.transition_to(IncidentStatus(name))


def _note_timeline(incident, note: str):
    timeline = list(incident.status_timeline or [])
    if timeline:
        timeline[-1] = {**timeline[-1], "note": note[:300]}
        incident.status_timeline = timeline


# ─── Dependencies (replaced by fakes in tests) ───

@dataclass
class Deps:
    github: Callable[[], tuple]                       # () -> (gateway | None, info)
    kube_runner: Callable = dep.subprocess_runner     # (argv, timeout) -> CommandResult
    http_get: Callable[[str], int] = None             # url -> status code
    prom_query: Callable[[str], list] = None          # PromQL -> vector result
    schedule: Callable[[str, int], None] = None       # (run_id, delay_seconds) -> None
    env: dict = field(default_factory=lambda: dict(os.environ))
    now: Callable[[], datetime.datetime] = _utcnow


def default_deps(schedule) -> Deps:
    import requests

    from engine.github_gateway import gateway_from_config

    def http_get(url):
        return requests.get(url, timeout=5).status_code

    def prom_query(query):
        base = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
        resp = requests.get(f"{base}/api/v1/query", params={"query": query}, timeout=5)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("result", [])

    return Deps(github=gateway_from_config, http_get=http_get, prom_query=prom_query, schedule=schedule)


# ─── Human decisions ───

def _load_incident(db, incident_uuid, lock=False):
    from api.models import Incident

    query = db.query(Incident).filter(Incident.id == incident_uuid)
    incident = (query.with_for_update() if lock else query).first()
    if incident is None:
        raise WorkflowError("INCIDENT_NOT_FOUND", "incident not found")
    return incident


def _run_for_proposal(db, proposal_id):
    from api.models import RemediationRun

    return db.query(RemediationRun).filter(RemediationRun.proposal_id == proposal_id).first()


def _latest_run(db, incident_id):
    from api.models import RemediationRun

    return (db.query(RemediationRun).filter(RemediationRun.incident_id == incident_id)
            .order_by(RemediationRun.created_at.desc()).first())


def _approvable(db, incident, proposal_uuid):
    """The latest proposal if it is an intact VALIDATED proposal for the current diagnosis."""
    row, status, _ = get_latest_proposal(db, incident.id)
    if row is None or str(row.id) != str(proposal_uuid):
        raise WorkflowError("PROPOSAL_NOT_LATEST", "only the incident's latest proposal can be decided")
    if status != VALIDATED:
        raise WorkflowError("PROPOSAL_NOT_VALIDATED", f"proposal status is {status}")
    if incident.status.value != "DIAGNOSED":
        raise WorkflowError("INCIDENT_NOT_DIAGNOSED", f"incident status is {incident.status.value}")
    current = report_sha256(incident.ai_report) if isinstance(incident.ai_report, dict) else None
    if not current or (row.source_report or {}).get("sha256") != current:
        raise WorkflowError("DIAGNOSIS_CHANGED", "the proposal was built from a different diagnosis")
    try:
        target = dep.target_from_proposal(row.target_ref, row.proposed_values)
    except dep.DeployBlocked as exc:
        raise WorkflowError(exc.code, exc.detail)
    if target.namespace in dep.RESTRICTED_NAMESPACES:
        raise WorkflowError("RESTRICTED_NAMESPACE", f"namespace {target.namespace} is protected")
    return row, target


def decide(session_factory, incident_uuid, proposal_uuid, verdict: str, actor: dict,
           reason: Optional[str] = None, now=None) -> tuple:
    """Records a human APPROVED/REJECTED decision on one proposal. Returns (run dict, changed).

    Idempotent: a second decision on the same proposal returns the first one unchanged.
    """
    from api.models import RemediationRun

    assert verdict in (APPROVED, REJECTED)
    now = now or _utcnow()
    reason = (reason or "").strip()[:500] or None
    if verdict == REJECTED and not reason:
        raise WorkflowError("REASON_REQUIRED", "a rejection needs a short reason")
    db = session_factory()
    try:
        incident = _load_incident(db, incident_uuid, lock=True)
        existing = _run_for_proposal(db, proposal_uuid)
        if existing is not None:
            if existing.stage == REJECTED and verdict == APPROVED or existing.stage != REJECTED and verdict == REJECTED:
                raise WorkflowError("ALREADY_DECIDED", f"this proposal was already {existing.stage.lower()}")
            return serialise_run(existing), False
        if verdict == APPROVED and _system_mode(db) == "DISABLED":
            raise WorkflowError("SYSTEM_DISABLED", "kill switch is DISABLED; approvals are refused")
        row, target = _approvable(db, incident, proposal_uuid)
        who = clean_actor(actor)
        run = RemediationRun(
            incident_id=incident.id, proposal_id=row.id, report_sha256=row.source_report["sha256"],
            diff_sha256=row.diff_sha256, target_key=f"{target.namespace}/Deployment/{target.name}",
            stage=verdict, decided_by=who, decision_reason=reason, decided_at=now, created_at=now,
            updated_at=now, finished_at=now if verdict == REJECTED else None,
            audit_log=[_event(f"HUMAN_{verdict}", now, by=who, reason=reason, proposal_id=str(row.id),
                              diff_sha256=row.diff_sha256)])
        db.add(run)
        if verdict == APPROVED:
            _move(incident, "POLICY_APPROVED")
            _note_timeline(incident, f"remediation approved ({'authenticated ' + who['identity'] if who['authenticated'] else 'unauthenticated local approval'})")
        else:
            _move(incident, "ESCALATED")
            _note_timeline(incident, f"remediation rejected: {reason}")
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return serialise_run(_run_for_proposal(db, proposal_uuid)), False
        db.refresh(run)
        logger.info(f"[TRACE:{incident.id}] [REMEDIATION] human {verdict} for proposal {row.id} (run {run.id})")
        return serialise_run(run), True
    finally:
        db.close()


def close_incident(session_factory, incident_uuid, actor: dict, reason: Optional[str], now=None) -> dict:
    """A human closes an incident without automated remediation (DIAGNOSED/ESCALATED -> RESOLVED)."""
    now = now or _utcnow()
    reason = (reason or "").strip()[:500]
    if not reason:
        raise WorkflowError("REASON_REQUIRED", "closing an incident needs a short reason")
    db = session_factory()
    try:
        incident = _load_incident(db, incident_uuid, lock=True)
        if incident.status.value not in ("DIAGNOSED", "ESCALATED"):
            raise WorkflowError("NOT_CLOSABLE", f"incident status is {incident.status.value}")
        run = _latest_run(db, incident.id)
        if run is not None and run.stage not in FINAL_STAGES:
            raise WorkflowError("RUN_ACTIVE", f"remediation run is {run.stage}")
        who = clean_actor(actor)
        _move(incident, "ESCALATED", "RESOLVED")
        by = who["identity"] if who["authenticated"] else "unauthenticated local user"
        _note_timeline(incident, f"closed manually by {by} without automated remediation: {reason}")
        db.commit()
        return {"incident_id": str(incident.id), "status": incident.status.value}
    finally:
        db.close()


# ─── Steps ───

def _claim(session_factory, run_uuid, now, stale_s=STEP_STALE_S):
    """Atomically claims the run's next step. Returns (run dict, token) or (run dict, None)."""
    from api.models import RemediationRun

    db = session_factory()
    try:
        run = db.query(RemediationRun).filter(RemediationRun.id == run_uuid).with_for_update().first()
        if run is None:
            raise WorkflowError("RUN_NOT_FOUND", "remediation run not found")
        if run.stage in FINAL_STAGES or run.stage == REJECTED:
            return serialise_run(run), None
        stale = now - datetime.timedelta(seconds=stale_s)
        if run.step_token and run.step_started_at and run.step_started_at > stale:
            return serialise_run(run), None
        token = uuid.uuid4().hex
        claimed = (db.query(RemediationRun)
                   .filter(RemediationRun.id == run.id, RemediationRun.stage == run.stage,
                           (RemediationRun.step_token.is_(None)) | (RemediationRun.step_token == run.step_token))
                   .update({"step_token": token, "step_started_at": now}, synchronize_session=False))
        db.commit()
        return serialise_run(run), (token if claimed == 1 else None)
    finally:
        db.close()


@dataclass
class Outcome:
    stage: Optional[str] = None                 # new stage, or None to keep it
    updates: dict = field(default_factory=dict) # column -> value
    events: list = field(default_factory=list)
    incident: tuple = ()                        # incident transitions to apply
    note: Optional[str] = None                  # note on the last timeline entry
    blocked: Optional[tuple] = None             # (code, detail): nothing changed, may retry
    next_in: Optional[int] = None               # schedule the next step after N seconds


def _finish(session_factory, run_uuid, token, outcome: Outcome, now) -> dict:
    """Writes a step's outcome if this worker still holds the claim."""
    from api.models import RemediationRun

    db = session_factory()
    try:
        run = db.query(RemediationRun).filter(RemediationRun.id == run_uuid).with_for_update().first()
        if run is None or run.step_token != token:
            logger.warning(f"[REMEDIATION] run {run_uuid}: claim lost, step result discarded")
            return serialise_run(run) if run else {}
        incident = _load_incident(db, run.incident_id, lock=True)
        for key, value in outcome.updates.items():
            setattr(run, key, value)
        events = list(outcome.events)
        if outcome.blocked:
            run.blocked_code, run.blocked_detail = outcome.blocked[0], outcome.blocked[1][:500]
            events.append(_event("STEP_BLOCKED", now, stage=run.stage, code=outcome.blocked[0],
                                 detail=outcome.blocked[1][:300]))
        else:
            run.blocked_code = run.blocked_detail = None
        if outcome.stage and outcome.stage != run.stage:
            events.append(_event("STAGE", now, frm=run.stage, to=outcome.stage))
            run.stage = outcome.stage
            if run.stage in FINAL_STAGES:
                run.finished_at = now
                run.deploy_lock = None
        if outcome.incident:
            try:
                _move(incident, *outcome.incident)
            except ValueError as exc:   # the incident was moved elsewhere: record, never lose the run outcome
                events.append(_event("INCIDENT_STATE_CONFLICT", now, wanted=list(outcome.incident),
                                     detail=str(exc)[:300]))
        if outcome.note:
            _note_timeline(incident, outcome.note)
        run.audit_log = _append(run.audit_log, *events)
        run.step_token, run.step_started_at, run.updated_at = None, None, now
        db.commit()
        return serialise_run(run)
    finally:
        db.close()


def _set_lock(session_factory, run_uuid, value: Optional[bool]) -> bool:
    """Takes (True) or releases (None) the per-target deployment lock. False if another run holds it."""
    from api.models import RemediationRun

    db = session_factory()
    try:
        db.query(RemediationRun).filter(RemediationRun.id == run_uuid).update(
            {"deploy_lock": value}, synchronize_session=False)
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    finally:
        db.close()


def _set_stage_now(session_factory, run_uuid, token, stage, now, **updates):
    """Records an intermediate stage (e.g. DEPLOYING) while the step is still running."""
    from api.models import RemediationRun

    db = session_factory()
    try:
        run = db.query(RemediationRun).filter(RemediationRun.id == run_uuid, RemediationRun.step_token == token).first()
        if run is None:
            return False
        run.audit_log = _append(run.audit_log, _event("STAGE", now, frm=run.stage, to=stage))
        run.stage, run.updated_at = stage, now
        for key, value in updates.items():
            setattr(run, key, value)
        db.commit()
        return True
    finally:
        db.close()


def _load_context(session_factory, run_uuid):
    from api.models import PatchProposal, RemediationRun

    db = session_factory()
    try:
        run = db.query(RemediationRun).filter(RemediationRun.id == run_uuid).first()
        proposal = db.query(PatchProposal).filter(PatchProposal.id == run.proposal_id).first()
        incident = _load_incident(db, run.incident_id)
        snap = {"incident_id": str(incident.id), "alert_name": incident.alert_name,
                "incident_status": incident.status.value, "project_id": incident.project_id,
                "ai_report": incident.ai_report, "system_mode": _system_mode(db)}
        db.expunge_all()
        return run, proposal, snap
    finally:
        db.close()


def _approved_proposal_problem(run, proposal) -> Optional[str]:
    """Every step re-checks that the stored proposal is still the intact one that was approved."""
    from engine.patch_proposer import effective_status

    if proposal is None:
        return "the approved proposal no longer exists"
    status, reason = effective_status(proposal)
    if status != VALIDATED:
        return f"the approved proposal is now {status} ({reason})"
    if proposal.diff_sha256 != run.diff_sha256:
        return "the stored diff differs from the one that was approved"
    return None


def advance(session_factory, run_uuid, deps: Deps) -> dict:
    """Runs the next step of a remediation run (idempotent; safe to call repeatedly)."""
    now = deps.now()
    run_view, token = _claim(session_factory, run_uuid, now, step_stale_s(deps.env))
    if token is None:
        return run_view
    run, proposal, snap = _load_context(session_factory, run_uuid)
    # Stages that must keep being checked even while blocked (a change may be live in the cluster).
    keep_polling = {PR_OPEN: _pr_poll_s(deps), DEPLOYING: 30, VERIFYING: 30}.get(run.stage)
    try:
        problem = _approved_proposal_problem(run, proposal)
        if problem:
            outcome = Outcome(blocked=("PROPOSAL_CHANGED", problem), next_in=keep_polling)
        elif snap["system_mode"] == "DISABLED":
            outcome = Outcome(blocked=("SYSTEM_DISABLED", "kill switch is DISABLED"), next_in=keep_polling)
        else:
            step = {APPROVED: _publish_pr, PR_OPEN: _check_pr, READY_TO_DEPLOY: _deploy,
                    DEPLOYING: _recover_deploying, VERIFYING: _verify_round}.get(run.stage)
            outcome = (step(session_factory, run, proposal, snap, deps, token, now) if step
                       else Outcome(blocked=("NOTHING_TO_DO", f"stage {run.stage} has no step")))
    except Exception as exc:   # never leave a claim behind; keep checking stages with live changes
        logger.exception(f"[REMEDIATION] run {run_uuid} step {run.stage} crashed")
        outcome = Outcome(blocked=("STEP_ERROR", f"{exc.__class__.__name__}: {exc}"), next_in=keep_polling)
    result = _finish(session_factory, run_uuid, token, outcome, deps.now())
    if outcome.next_in is not None and deps.schedule:
        deps.schedule(str(run_uuid), outcome.next_in)
    return result


# ─── Stage 3: pull request ───

def _patched_content(proposal, base_bytes: bytes) -> bytes:
    """Rebuilds the approved file from the base file and the stored diff, and checks both hashes."""
    base_lf = base_bytes.replace(b"\r\n", b"\n")
    if git_blob_sha(base_lf) != (proposal.base or {}).get("git_blob_sha"):
        raise WorkflowError("BASE_CHANGED", "the manifest on the default branch is not the one the proposal was validated against")
    try:
        parsed = parse_unified_diff_strict(proposal.unified_diff, proposal.target_file)
        after = apply_unified_diff_strict(base_lf.decode("utf-8"), parsed).encode("utf-8")
    except (PatchDiffError, UnicodeDecodeError) as exc:
        raise WorkflowError("PATCH_MISMATCH", f"stored diff does not apply: {exc}")
    if git_blob_sha(after) != proposal.patched_git_blob_sha:
        raise WorkflowError("PATCH_MISMATCH", "rebuilt file does not match the validated patch")
    return after


def _shadow(snap, deps) -> bool:
    return snap["system_mode"] == "SHADOW" or _flag(deps.env, "SHADOW_MODE", "true")


def _publish_pr(session_factory, run, proposal, snap, deps, token, now) -> Outcome:
    gateway, info = deps.github()
    if gateway is None:
        if _flag(deps.env, "REMEDIATION_REQUIRE_PR", "true"):
            return Outcome(blocked=("GITHUB_NOT_CONFIGURED",
                                    f"GitHub is not configured (missing: {', '.join(info.get('missing', []))}); "
                                    "set GITHUB_TOKEN/GITHUB_REPO or REMEDIATION_REQUIRE_PR=false"))
        return Outcome(stage=READY_TO_DEPLOY, incident=("REMEDIATING",), next_in=0,
                       updates={"github": {"mode": "skipped", "reason": "REMEDIATION_REQUIRE_PR=false"}},
                       events=[_event("PR_SKIPPED", now, reason="REMEDIATION_REQUIRE_PR=false")],
                       note="pull request step skipped by configuration")
    branch = f"autofixops/remediation-{str(proposal.id)[:8]}"
    try:
        pr = gateway.find_pr(branch)
        base_branch = gateway.default_branch()
        base_commit = gateway.branch_head(base_branch)
        if pr is None:
            base_bytes, base_blob = gateway.get_file(proposal.target_file, base_commit)
            patched = _patched_content(proposal, base_bytes)
            head = gateway.branch_head(branch)
            if head is None:
                gateway.create_branch(branch, base_commit)
                commit_sha = gateway.put_file(proposal.target_file, patched, base_blob, branch, _commit_message(proposal, snap))
            else:   # a previous attempt created the branch: reuse it only if it holds exactly the patch
                current, current_blob = gateway.get_file(proposal.target_file, branch)
                if git_blob_sha(current.replace(b"\r\n", b"\n")) == proposal.patched_git_blob_sha:
                    commit_sha = head
                elif current_blob == base_blob:
                    commit_sha = gateway.put_file(proposal.target_file, patched, base_blob, branch,
                                                  _commit_message(proposal, snap))
                else:
                    return Outcome(blocked=("BRANCH_CONFLICT", f"branch {branch} has unexpected content"))
            pr = gateway.create_pr(_pr_title(proposal, snap, _shadow(snap, deps)), build_pr_body(run, proposal, snap),
                                   head=branch, base=base_branch, draft=_shadow(snap, deps))
        else:
            commit_sha = pr.get("head_sha")
    except WorkflowError as exc:
        return Outcome(stage=FAILED, incident=("REMEDIATING", "FAILED"), note=f"PR not created: {exc.code}",
                       events=[_event("PR_FAILED", now, code=exc.code, detail=exc.detail)],
                       updates={"blocked_code": exc.code})
    except Exception as exc:   # GitHubError and network errors: nothing merged, retry later
        return Outcome(blocked=(getattr(exc, "code", "GITHUB_ERROR"), getattr(exc, "detail", str(exc))))
    github = {"mode": "github", "repo": info.get("repo"), "branch": branch, "base_branch": base_branch,
              "base_commit_sha": base_commit, "commit_sha": commit_sha, "pr_number": pr["number"],
              "pr_url": pr["url"], "pr_state": pr["state"], "merged": pr["merged"], "draft": pr.get("draft"),
              "patched_blob_sha": proposal.patched_git_blob_sha, "opened_at": _iso(now), "last_checked_at": _iso(now)}
    return Outcome(stage=PR_OPEN, updates={"github": github}, incident=("REMEDIATING", "PENDING_PR_MERGE"),
                   events=[_event("PR_OPENED", now, number=pr["number"], url=pr["url"], draft=pr.get("draft"))],
                   note=f"pull request #{pr['number']} opened", next_in=_pr_poll_s(deps))


def _pr_poll_s(deps) -> int:
    try:
        return max(10, min(3600, int(deps.env.get("REMEDIATION_PR_POLL_SECONDS", 30))))
    except (TypeError, ValueError):
        return 30


_CI_OK = {"success", "skipped", "neutral"}


def ci_summary(runs: list) -> str:
    """success | failure | pending for the GitHub Actions runs of one commit."""
    if not runs:
        return "pending"
    if any(r.get("status") == "completed" and r.get("conclusion") not in _CI_OK for r in runs):
        return "failure"
    if all(r.get("status") == "completed" for r in runs):
        return "success"
    return "pending"


def _check_pr(session_factory, run, proposal, snap, deps, token, now) -> Outcome:
    gateway, _ = deps.github()
    if gateway is None:   # e.g. a worker started without the token: keep checking, change nothing
        return Outcome(blocked=("GITHUB_NOT_CONFIGURED", "GitHub credentials are not configured in this worker"),
                       next_in=_pr_poll_s(deps))
    github = dict(run.github or {})
    require_ci = _flag(deps.env, "REMEDIATION_REQUIRE_CI", "true")
    try:
        pr = gateway.get_pr(github["pr_number"])
        github.update(pr_state=pr["state"], merged=pr["merged"], last_checked_at=_iso(now))
        runs = gateway.ci_runs(pr["head_sha"]) if require_ci else []
        github.update(ci_required=require_ci, ci_status=ci_summary(runs) if require_ci else "not required",
                      ci_runs=[{k: r.get(k) for k in ("name", "status", "conclusion", "url")} for r in runs][:10],
                      ci_head_sha=pr["head_sha"])
        if pr["merged"] and require_ci and github["ci_status"] != "success":
            if github["ci_status"] == "failure":
                return Outcome(stage=FAILED, updates={"github": github}, incident=("FAILED",),
                               events=[_event("CI_FAILED", now, head_sha=pr["head_sha"])],
                               note="the PR was merged but GitHub Actions failed on it; not deployed")
            return Outcome(updates={"github": github}, next_in=_pr_poll_s(deps),
                           blocked=("CI_PENDING", "merged; waiting for GitHub Actions to finish on the PR commit"))
        if pr["merged"]:
            content, _ = gateway.get_file(proposal.target_file, pr["merge_commit_sha"])
            if git_blob_sha(content.replace(b"\r\n", b"\n")) != proposal.patched_git_blob_sha:
                github["merged_content_matches"] = False
                return Outcome(stage=FAILED, updates={"github": github}, incident=("FAILED",),
                               events=[_event("MERGED_CONTENT_MISMATCH", now, merge_commit=pr["merge_commit_sha"])],
                               note="merged manifest differs from the approved patch; not deployed")
            github.update(merged_content_matches=True, merge_commit_sha=pr["merge_commit_sha"], merged_seen_at=_iso(now))
            return Outcome(stage=READY_TO_DEPLOY, updates={"github": github}, next_in=0,
                           events=[_event("PR_MERGED", now, merge_commit=pr["merge_commit_sha"])],
                           note=f"pull request #{github['pr_number']} merged")
        if pr["state"] == "closed":
            return Outcome(stage=FAILED, updates={"github": github}, incident=("FAILED",),
                           events=[_event("PR_CLOSED_UNMERGED", now)], note="pull request closed without merge")
    except Exception as exc:
        return Outcome(updates={"github": github}, next_in=_pr_poll_s(deps),
                       blocked=(getattr(exc, "code", "GITHUB_ERROR"), getattr(exc, "detail", str(exc))))
    return Outcome(updates={"github": github}, next_in=_pr_poll_s(deps))   # still open: check again later


# ─── Stage 4: deployment and verification ───

def _target(proposal):
    return dep.target_from_proposal(proposal.target_ref, proposal.proposed_values)


def _deploy(session_factory, run, proposal, snap, deps, token, now) -> Outcome:
    if _shadow(snap, deps):
        return Outcome(blocked=("SHADOW_MODE", "shadow mode never deploys (set SHADOW_MODE=false and system mode ACTIVE)"))
    try:
        cfg = dep.deploy_config(deps.env)
        vcfg = rv.verify_config(deps.env)
        target = _target(proposal)
        dep.check_allowed(cfg, target)
    except dep.DeployBlocked as exc:
        return Outcome(blocked=(exc.code, exc.detail))
    if not _set_lock(session_factory, run.id, True):
        return Outcome(blocked=("TARGET_BUSY", f"another remediation is deploying to {target.key}"), next_in=30)
    if not _set_stage_now(session_factory, run.id, token, DEPLOYING, now):
        _set_lock(session_factory, run.id, None)   # another worker took the step over
        return Outcome()
    kube = dep.Kubectl(cfg, deps.kube_runner)
    deployment = {"mode": "kubectl", "context": cfg.context, "target": target.key, "container": target.container,
                  "resource": target.resource, "from": target.old_value, "to": target.new_value,
                  "started_at": _iso(now)}
    try:
        record = dep.apply_change(kube, target)
    except dep.DeployBlocked as exc:   # nothing changed in the cluster
        _set_lock(session_factory, run.id, None)
        deployment.update(error=exc.code, detail=exc.detail, commands=kube.commands)
        final = exc.code in ("DRIFT_DETECTED", "DRY_RUN_REFUSED", "PATCH_REFUSED", "TARGET_NOT_FOUND")
        if final:
            return Outcome(stage=FAILED, updates={"deployment": deployment}, incident=("FAILED",),
                           events=[_event("DEPLOY_REFUSED", deps.now(), code=exc.code, detail=exc.detail)],
                           note=f"deployment refused before any change: {exc.code}")
        return Outcome(stage=READY_TO_DEPLOY, updates={"deployment": deployment}, blocked=(exc.code, exc.detail))
    except dep.DeployFailed as exc:    # patched, but the rollout did not complete: undo it
        deployment.update(exc.record, error=exc.code, detail=exc.detail, commands=kube.commands)
        return _rollback(session_factory, run, proposal, snap, deps, kube, target,
                         exc.record["previous"]["container"]["index"], deployment, None, f"rollout failed: {exc.detail}")
    except Exception as exc:           # crash/timeout mid-step: decide from the live cluster state
        deployment.update(commands=kube.commands)
        return _reconcile(session_factory, run, proposal, snap, deps, kube, target, deployment,
                          f"deploy step interrupted ({exc.__class__.__name__})")
    deployment.update(record, finished_at=_iso(deps.now()), commands=kube.commands)
    verification = {"started_at": _iso(deps.now()), "stable_since": None, "rounds": [], "baseline_restarts": None,
                    "interval_s": vcfg.interval_s, "stabilization_s": vcfg.stabilization_s, "timeout_s": vcfg.timeout_s}
    return Outcome(stage=VERIFYING, updates={"deployment": deployment, "verification": verification},
                   events=[_event("DEPLOYED", deps.now(), target=target.key, value=target.new_value)],
                   note=f"deployed {target.resource} limit {target.old_value} -> {target.new_value} to {target.key}",
                   next_in=vcfg.interval_s)


def _reconcile(session_factory, run, proposal, snap, deps, kube, target, deployment, why) -> Outcome:
    """The deploy step ended without a clear result. Reads the live limit and either rolls the
    change back (it is live but its rollout was never confirmed), returns to READY_TO_DEPLOY
    (nothing changed), or fails (someone else changed it). While the cluster cannot be read the
    run stays DEPLOYING, keeps the target lock and is checked again."""
    try:
        live = kube.get_deployment(target)
        index = dep.container_index(live, target.container)
        value = dep.live_limit(live, index, target.resource)
    except Exception as exc:
        return Outcome(stage=DEPLOYING, updates={"deployment": deployment}, next_in=30,
                       blocked=("DEPLOY_STATE_UNKNOWN", f"{why}; live state unreadable ({exc.__class__.__name__})"))
    if value == target.new_value:
        return _rollback(session_factory, run, proposal, snap, deps, kube, target, index, deployment, None,
                         f"{why}; the change is live but its rollout was not confirmed")
    if value == target.old_value:
        _set_lock(session_factory, run.id, None)
        return Outcome(stage=READY_TO_DEPLOY, updates={"deployment": deployment},
                       blocked=("DEPLOY_INTERRUPTED", f"{why}; the live value is unchanged, safe to retry"))
    return Outcome(stage=FAILED, updates={"deployment": deployment}, incident=("FAILED",),
                   events=[_event("DRIFT_DETECTED", deps.now(), live=str(value))],
                   note=f"{why}; live {target.resource} limit is {value}, changed by someone else - manual review needed")


def _recover_deploying(session_factory, run, proposal, snap, deps, token, now) -> Outcome:
    """A run found in DEPLOYING was interrupted (worker crash or stale claim)."""
    try:
        cfg = dep.deploy_config(deps.env)
        target = _target(proposal)
    except dep.DeployBlocked as exc:
        return Outcome(blocked=(exc.code, exc.detail), next_in=60)
    kube = dep.Kubectl(cfg, deps.kube_runner)
    return _reconcile(session_factory, run, proposal, snap, deps, kube, target, dict(run.deployment or {}),
                      "previous deploy step did not finish")


def _parse(ts: Optional[str]):
    return datetime.datetime.fromisoformat(ts.rstrip("Z")) if ts else None


def _verify_round(session_factory, run, proposal, snap, deps, token, now) -> Outcome:
    try:
        cfg = dep.deploy_config(deps.env)
        vcfg = rv.verify_config(deps.env)
        target = _target(proposal)
    except dep.DeployBlocked as exc:
        return Outcome(blocked=(exc.code, exc.detail), next_in=60)
    kube = dep.Kubectl(cfg, deps.kube_runner)
    verification = dict(run.verification or {})
    result = rv.run_round(kube, target, vcfg, snap["alert_name"], deps.http_get, deps.prom_query,
                          verification.get("baseline_restarts"))
    if verification.get("baseline_restarts") is None and result["restarts"]:
        verification["baseline_restarts"] = result["restarts"]
    rounds = list(verification.get("rounds") or []) + [{"at": _iso(now), "ok": result["ok"], "checks": result["checks"]}]
    verification["rounds"] = rounds[-MAX_ROUNDS_KEPT:]
    verification["round_count"] = int(verification.get("round_count") or 0) + 1
    started = _parse(verification.get("started_at")) or now
    last = _parse(verification.get("last_round_at"))
    verification["last_round_at"] = _iso(now)
    if last is not None and (now - last).total_seconds() > 2 * vcfg.interval_s:
        verification["stable_since"] = None   # unobserved time never counts as stable
    if result["ok"]:
        stable_since = _parse(verification.get("stable_since")) or now
        verification["stable_since"] = _iso(stable_since)
        if (now - stable_since).total_seconds() >= vcfg.stabilization_s:
            verification.update(result="PASSED", finished_at=_iso(now))
            return Outcome(stage=RESOLVED, updates={"verification": verification},
                           incident=("VERIFIED", "RESOLVED"),
                           events=[_event("VERIFIED", now, rounds=verification["round_count"])],
                           note=f"recovery verified: all checks passed for {vcfg.stabilization_s}s")
    else:
        verification["stable_since"] = None
    if (now - started).total_seconds() >= vcfg.timeout_s:
        verification.update(result="FAILED", finished_at=_iso(now))
        failing = [c["name"] for c in result["checks"] if not c["ok"]]
        deployment = dict(run.deployment or {})
        index = ((deployment.get("previous") or {}).get("container") or {}).get("index", 0)
        return _rollback(session_factory, run, proposal, snap, deps, kube, target, index, deployment,
                         verification, f"verification timed out; failing checks: {', '.join(failing)}")
    return Outcome(updates={"verification": verification}, next_in=vcfg.interval_s)


def _rollback(session_factory, run, proposal, snap, deps, kube, target, index, deployment, verification, why) -> Outcome:
    now = deps.now()
    result = dep.rollback_change(kube, target, index)
    result.update(reason=why, at=_iso(now))
    if result["ok"] and (run.github or {}).get("merged"):
        result["git_revert"] = _revert_pr(run, proposal, snap, deps)
    updates = {"deployment": deployment, "rollback": result}
    if verification is not None:
        updates["verification"] = verification
    stage = ROLLED_BACK if result["ok"] else ROLLBACK_FAILED
    note = (f"remediation failed ({why}); rolled back to {target.old_value}" if result["ok"]
            else f"remediation failed ({why}); ROLLBACK FAILED - manual action required")
    return Outcome(stage=stage, updates=updates, incident=("FAILED",), note=note,
                   events=[_event("ROLLED_BACK" if result["ok"] else "ROLLBACK_FAILED", now, reason=why[:300])])


def _revert_pr(run, proposal, snap, deps) -> dict:
    """After a rollback, opens a PR restoring the manifest so Git matches the cluster again (best effort)."""
    try:
        gateway, _ = deps.github()
        if gateway is None:
            return {"created": False, "reason": "GitHub not configured"}
        github = run.github or {}
        branch = f"autofixops/revert-{str(proposal.id)[:8]}"
        existing = gateway.find_pr(branch)
        if existing:
            return {"created": True, "number": existing["number"], "url": existing["url"]}
        base_branch = gateway.default_branch()
        head = gateway.branch_head(base_branch)
        original, _ = gateway.get_file(proposal.target_file, github["base_commit_sha"])
        if git_blob_sha(original.replace(b"\r\n", b"\n")) != (proposal.base or {}).get("git_blob_sha"):
            return {"created": False, "reason": "recorded base commit does not hold the original manifest; revert manually"}
        _, current_blob = gateway.get_file(proposal.target_file, head)
        gateway.create_branch(branch, head)
        gateway.put_file(proposal.target_file, original, current_blob, branch,
                         f"revert(autofixops): restore {proposal.target_file} after failed verification")
        pr = gateway.create_pr(f"[AutoFixOps] Revert remediation for incident {snap['incident_id'][:8]}",
                               f"Verification failed and the cluster change was rolled back.\n\n"
                               f"This restores `{proposal.target_file}` to its state before PR #{github.get('pr_number')}"
                               f" so that Git matches the cluster again.", head=branch, base=base_branch, draft=False)
        return {"created": True, "number": pr["number"], "url": pr["url"]}
    except Exception as exc:
        return {"created": False, "reason": f"{exc.__class__.__name__}: {getattr(exc, 'detail', exc)}"[:300]}


# ─── PR text ───

def _pr_title(proposal, snap, shadow: bool) -> str:
    values = proposal.proposed_values or {}
    resource = (values.get("field_path") or "").rsplit(".", 1)[-1]
    return (f"{'[SHADOW] ' if shadow else ''}[AutoFixOps] {proposal.action_type}: {resource} limit "
            f"{values.get('old_value')} -> {values.get('new_value')} (incident {snap['incident_id'][:8]})")


def _commit_message(proposal, snap) -> str:
    values = proposal.proposed_values or {}
    return (f"fix(autofixops): {proposal.action_type} {values.get('old_value')} -> {values.get('new_value')} "
            f"for incident {snap['incident_id'][:8]}")


def _md(text, limit=400) -> str:
    """Plain one-line text for Markdown (model text is shown as data, never as markup)."""
    text = " ".join(str(text or "").split())[:limit]
    return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")


def build_pr_body(run, proposal, snap) -> str:
    report = snap.get("ai_report") or {}
    ref, values = proposal.target_ref or {}, proposal.proposed_values or {}
    evidence = proposal.evidence or {}
    who = run.decided_by or {}
    approver = who.get("identity") if who.get("authenticated") else "unauthenticated local approval (no identity available)"
    facts = "\n".join(f"- `{f.get('id')}` ({f.get('source')}): {_md(f.get('text'))}"
                      for f in evidence.get("facts_offered") or []) or "- (none)"
    runbooks = "\n".join(f"- {_md(rb.get('title') or rb.get('runbook_id'))} — {_md(rb.get('source'), 200)}"
                         for rb in evidence.get("runbooks_offered") or []) or "- (none)"
    checks = "\n".join(f"| {_md(s.get('name') or s.get('check'))} | {_md(s.get('status'))} |"
                       for s in (proposal.validation or {}).get("steps") or [])
    return f"""## AutoFixOps remediation

> Generated by AutoFixOps from a sandbox-validated Stage 2 proposal and a recorded human approval.
> Merging this PR is the GitOps approval for deployment: AutoFixOps deploys only after it sees this exact change merged.

### Incident
| | |
|---|---|
| Incident | `{snap['incident_id']}` |
| Alert | `{_md(snap.get('alert_name'))}` |
| Classification | `{_md(report.get('incident_type'))}` |
| Diagnosis confidence | {report.get('confidence')} |

### Diagnosis (AI analysis, inference)
{_md(report.get('root_cause_analysis'), 800)}

### Evidence offered to the proposer (code-derived facts)
{facts}

### Official runbook guidance
{runbooks}

### Proposed change
`{_md(proposal.target_file)}` — Deployment `{_md(ref.get('namespace'))}/{_md(ref.get('name'))}`, container `{_md(ref.get('container'))}`:
`{_md(values.get('field_path'))}`: **{_md(values.get('old_value'))} → {_md(values.get('new_value'))}**

```diff
{proposal.unified_diff}```

### Validation results (all mandatory checks passed)
| Check | Result |
|---|---|
{checks}

### Approval
- Decision: **APPROVED** at {_iso(run.decided_at)} by {_md(approver, 200)}
- Reason: {_md(run.decision_reason or '(none given)')}
- Proposal `{proposal.id}`, diff sha256 `{proposal.diff_sha256}`, patched blob `{proposal.patched_git_blob_sha}`

### Rollback
Previous value `{_md(values.get('old_value'))}` is recorded. If post-deployment verification fails, AutoFixOps restores it and opens a revert PR.
"""


# ─── Views ───

def serialise_run(run) -> dict:
    if run is None:
        return None
    return {
        "id": str(run.id), "incident_id": str(run.incident_id), "proposal_id": str(run.proposal_id),
        "stage": run.stage, "final": run.stage in FINAL_STAGES, "target_key": run.target_key,
        "blocked_code": run.blocked_code, "blocked_detail": run.blocked_detail,
        "decided_by": run.decided_by, "decision_reason": run.decision_reason, "decided_at": _iso(run.decided_at),
        "step_in_progress": bool(run.step_token), "github": run.github, "deployment": run.deployment,
        "verification": run.verification, "rollback": run.rollback, "audit_log": run.audit_log or [],
        "created_at": _iso(run.created_at), "updated_at": _iso(run.updated_at), "finished_at": _iso(run.finished_at),
    }


def config_summary(env=None) -> dict:
    """What is configured (never secrets), so the dashboard can explain blocked steps."""
    env = os.environ if env is None else env
    summary = {"require_pr": _flag(env, "REMEDIATION_REQUIRE_PR", "true"),
               "shadow_mode_env": _flag(env, "SHADOW_MODE", "true"),
               "github_repo_set": bool((env.get("GITHUB_REPO") or "").strip()),
               "deploy_mode": (env.get("DEPLOY_MODE") or "disabled").strip().lower()}
    try:
        cfg = dep.deploy_config(env)
        summary.update(deploy_ready=True, kube_context=cfg.context, allowed_targets=sorted(cfg.allowed_targets))
    except dep.DeployBlocked as exc:
        summary.update(deploy_ready=False, deploy_problem=f"{exc.code}: {exc.detail}")
    try:
        vcfg = rv.verify_config(env)
        summary.update(verify_ready=True, health_url=vcfg.health_url, stabilization_s=vcfg.stabilization_s,
                       timeout_s=vcfg.timeout_s, interval_s=vcfg.interval_s)
    except dep.DeployBlocked as exc:
        summary.update(verify_ready=False, verify_problem=f"{exc.code}: {exc.detail}")
    return summary


def incident_timeline(db, incident) -> list:
    """Incident-to-recovery timeline: status changes plus remediation events, oldest first."""
    from api.models import PatchProposal, PatchRoutingDecision, RemediationRun

    items = [{"at": _iso(incident.created_at), "kind": "ALERT_RECEIVED",
              "label": f"Alert {incident.alert_name} received", "source": "alertmanager/api"}]
    for entry in incident.status_timeline or []:
        ts = entry.get("timestamp")
        items.append({"at": (ts + "Z") if ts and not ts.endswith("Z") else ts, "kind": f"STATUS_{entry.get('status')}",
                      "label": f"Incident {entry.get('status')}", "note": entry.get("note"), "source": "incident"})
    for d in db.query(PatchRoutingDecision).filter(PatchRoutingDecision.incident_id == incident.id).all():
        for e in d.audit_log or []:
            items.append({"at": e.get("at"), "kind": f"ROUTING_{e.get('event')}", "label": f"Stage 2: {e.get('event')}",
                          "source": "routing"})
    for p in db.query(PatchProposal).filter(PatchProposal.incident_id == incident.id).all():
        if p.completed_at:
            items.append({"at": _iso(p.completed_at), "kind": f"PROPOSAL_{p.status}",
                          "label": f"Patch proposal {p.status}" + (f" ({p.reason_code})" if p.reason_code else ""),
                          "source": "proposer"})
    for r in db.query(RemediationRun).filter(RemediationRun.incident_id == incident.id).all():
        for e in r.audit_log or []:
            if e.get("event") == "STAGE":
                label = f"Remediation {e.get('frm')} → {e.get('to')}"
            else:
                label = f"Remediation: {e.get('event')}" + (f" ({e.get('code')})" if e.get("code") else "")
            items.append({"at": e.get("at"), "kind": f"REMEDIATION_{e.get('event')}", "label": label,
                          "source": "remediation"})
    return sorted((i for i in items if i.get("at")), key=lambda i: i["at"])


def remediation_view(db, incident_uuid, env=None) -> dict:
    incident = _load_incident(db, incident_uuid)
    row, status, reason = get_latest_proposal(db, incident.id)
    run = _latest_run(db, incident.id)
    proposal = None
    if row is not None:
        current = report_sha256(incident.ai_report) if isinstance(incident.ai_report, dict) else None
        proposal = {"id": str(row.id), "effective_status": status, "reason_code": reason,
                    "matches_current_diagnosis": bool(current) and (row.source_report or {}).get("sha256") == current,
                    "proposed_values": row.proposed_values if status == VALIDATED else None,
                    "target_file": row.target_file}
    can_decide = bool(proposal and status == VALIDATED and proposal["matches_current_diagnosis"]
                      and incident.status.value == "DIAGNOSED"
                      and (run is None or str(run.proposal_id) != proposal["id"]))
    return {"incident_id": str(incident.id), "incident_status": incident.status.value,
            "proposal": proposal, "can_decide": can_decide, "run": serialise_run(run),
            "can_close": incident.status.value in ("DIAGNOSED", "ESCALATED") and (run is None or run.stage in FINAL_STAGES),
            "config": config_summary(env), "timeline": incident_timeline(db, incident)}
