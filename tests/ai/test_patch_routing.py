"""
AI Drill: Confidence-Based Patch Routing and Human Approval (Stage 2)
=====================================================================
confidence >= 0.80 -> AUTOMATIC_AI (generate without asking)
confidence <  0.80 -> HUMAN_APPROVAL_REQUIRED (generate only after a recorded approval)

The route is computed by the backend from the stored diagnosis report; clients
cannot send a confidence or a route. Both routes run the same proposer and the
same safety checks. Nothing is ever applied, committed, pushed or deployed.

Offline only: in-memory SQLite, stub LLMs, temporary repos and a recording
dispatcher instead of Celery. No Postgres, Redis, Ollama, OpenAI or GitHub.
"""
import ast
import copy
import datetime
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda

import test_patch_proposals as base
from api import patch_proposals as api_module
from api.models import Incident, IncidentStatus, PatchProposal, PatchRoutingDecision, SystemConfig, SystemMode
from engine import patch_proposer as pp
from engine import patch_routing as routing
from engine import routing_policy as policy
from engine.routing_policy import RoutingError

FAST = base.FAST


# ─── Helpers ───

class Dispatcher:
    """Records dispatches; optionally fails, or runs the generation inline like a worker would."""

    def __init__(self, fail=False, run_inline=None):
        self.calls, self.fail, self.run_inline = [], fail, run_inline

    def __call__(self, incident_id, decision_id, claim):
        self.calls.append((incident_id, decision_id, claim))
        if self.fail:
            raise ConnectionError("broker unreachable")
        if self.run_inline:
            self.run_inline(incident_id, decision_id, claim)


def _incident(SF, confidence=0.9, **kw):
    report = kw.pop("report", None) or base._report(confidence=confidence)
    return base._add_incident(SF, report=report, **kw)


def _decisions(SF):
    db = SF()
    try:
        return db.query(PatchRoutingDecision).all()
    finally:
        db.close()


def _expect_routing_error(code, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except RoutingError as exc:
        assert exc.code == code, (exc.code, exc.detail)
        return exc
    raise AssertionError(f"expected RoutingError {code}")


def _run(SF, incident_id, llm=None):
    llm = llm or base.StubLLM()
    with base.temp_repo() as root:
        result = pp.run_for_incident(SF, incident_id, root, llm=llm.runnable, env={}, settings=FAST)
    return result, llm


def _inline_runner(SF, llm_factory=base.StubLLM):
    """A dispatcher target that runs the worker-side generation against a temp repo."""
    def run(incident_id, decision_id, claim):
        with base.temp_repo() as root:
            routing.run_generation(SF, incident_id, decision_id, claim, root, llm=llm_factory().runnable, env={},
                                   settings=FAST)
    return run


class JsonClient(TestClient):
    """Sends an empty JSON object when a POST names no body, as the dashboard's fetchAPI does."""

    def post(self, url, **kw):
        if not any(k in kw for k in ("json", "content", "data", "files")):
            kw["json"] = {}
        return super().post(url, **kw)


def _client(SF, dispatcher):
    app = FastAPI()
    app.include_router(api_module.router)
    app.dependency_overrides[api_module.get_session_factory] = lambda: SF
    app.dependency_overrides[api_module.get_dispatcher] = lambda: dispatcher
    return JsonClient(app)


# ─── Routing policy ───

def test_routing_threshold_boundaries():
    assert policy.ROUTING_THRESHOLD == 0.80
    cases = {0.7999: policy.HUMAN_APPROVAL_REQUIRED, 0.79999999: policy.HUMAN_APPROVAL_REQUIRED,
             0.8: policy.AUTOMATIC_AI, 0.8000: policy.AUTOMATIC_AI, 0.8001: policy.AUTOMATIC_AI,
             0.0: policy.HUMAN_APPROVAL_REQUIRED, 1.0: policy.AUTOMATIC_AI, 1: policy.AUTOMATIC_AI}
    for confidence, route in cases.items():
        assert policy.route_for_confidence(confidence) == route, confidence
    invalid = {None: "CONFIDENCE_MISSING", float("nan"): "CONFIDENCE_INVALID", float("inf"): "CONFIDENCE_INVALID",
               float("-inf"): "CONFIDENCE_INVALID", True: "CONFIDENCE_INVALID", False: "CONFIDENCE_INVALID",
               "0.95": "CONFIDENCE_INVALID", 1.0001: "CONFIDENCE_INVALID", -0.01: "CONFIDENCE_INVALID"}
    for value, code in invalid.items():
        _expect_routing_error(code, policy.route_for_confidence, value)

    # The same boundaries through the stored incident (what the API and worker use).
    for confidence, route in ((0.7999, policy.HUMAN_APPROVAL_REQUIRED), (0.8000, policy.AUTOMATIC_AI),
                              (0.8001, policy.AUTOMATIC_AI)):
        SF = base._factory()
        decision = routing.ensure_decision(SF, _incident(SF, confidence))
        assert (decision["route"], decision["approval_required"]) == (route, route != policy.AUTOMATIC_AI)
        assert decision["decision"] == (policy.AUTO if route == policy.AUTOMATIC_AI else policy.PENDING)
    print("✅ PASS: 0.7999 → approval required; 0.8000 / 0.8001 → automatic; invalid confidences rejected")


def test_one_threshold_conflicting_configuration_refused():
    # Before: PATCH_PROPOSAL_MIN_CONFIDENCE=0.95 routed a 0.85 report AUTOMATIC_AI, then the proposer said
    # LOW_CONFIDENCE and approval was "not applicable" - a dead end. Now the conflict is refused up front.
    for env, project, source in (({"PATCH_PROPOSAL_MIN_CONFIDENCE": "0.95"}, None, "PATCH_PROPOSAL_MIN_CONFIDENCE"),
                                 ({}, {"confidence_threshold": 0.9}, "project confidence_threshold"),
                                 ({}, {"confidence_threshold": 0.5}, "project confidence_threshold")):
        SF = base._factory()
        incident_id = _incident(SF, 0.85, project=project)
        dispatcher = Dispatcher()
        with patch.dict(os.environ, env):
            exc = _expect_routing_error("CONFIG_INVALID", routing.request_generation, SF, incident_id, dispatcher)
            assert source in exc.detail and "0.80" in exc.detail, exc.detail
            _expect_routing_error("CONFIG_INVALID", routing.decide, SF, incident_id, policy.APPROVED, None)
            client = _client(SF, dispatcher)
            view = client.get(f"/api/v1/incidents/{incident_id}/patch-proposals").json()
            assert view["routing"] is None and view["routing_error"]["code"] == "CONFIG_INVALID"
            assert client.post(f"/api/v1/incidents/{incident_id}/patch-proposals").status_code == 409
            with base.temp_repo() as root:   # the proposer refuses the same configuration (CLI path)
                result = pp.run_for_incident(SF, incident_id, root, llm=base.StubLLM().runnable, env=dict(env))
            assert (result["status"], result["reason_code"]) == (pp.NOT_GENERATED, "CONFIG_INVALID")
        assert dispatcher.calls == [] and _decisions(SF) == []
    # A project threshold of exactly 0.80 (the column default) is consistent and routes normally.
    SF = base._factory()
    incident_id = _incident(SF, 0.8, project={"confidence_threshold": 0.80})
    assert routing.ensure_decision(SF, incident_id)["route"] == policy.AUTOMATIC_AI
    assert policy.threshold_conflict(0.8, "x") is None and policy.threshold_conflict(None, "x") is None
    for bad in (0.79, 0.81, float("nan"), True, "0.8"):
        assert policy.threshold_conflict(bad, "x"), bad
    print("✅ PASS: one 0.80 threshold; conflicting env/project thresholds refused by routing, API and proposer")


def test_malformed_reports_cannot_be_routed():
    nan, inf = float("nan"), float("inf")
    cases = {
        "confidence NaN": (base._report(confidence=nan), "CONFIDENCE_INVALID"),
        "confidence +inf": (base._report(confidence=inf), "CONFIDENCE_INVALID"),
        "confidence -inf": (base._report(confidence=-inf), "CONFIDENCE_INVALID"),
        "confidence missing": ({k: v for k, v in base._report().items() if k != "confidence"}, "CONFIDENCE_MISSING"),
        "confidence string": (base._report(confidence="0.99"), "CONFIDENCE_INVALID"),
        "confidence bool": (base._report(confidence=True), "CONFIDENCE_INVALID"),
        "risk_impact string": (base._report(risk_impact="HIGH"), "REPORT_INVALID"),
        "proposed_action string": (base._report(proposed_action="INCREASE_MEMORY_LIMIT"), "REPORT_INVALID"),
        "wrong report version": (base._report(report_version=2), "REPORT_INVALID"),
    }
    for name, (report, code) in cases.items():
        SF = base._factory()
        incident_id = _incident(SF, report=report)
        _expect_routing_error(code, routing.ensure_decision, SF, incident_id)
        assert _decisions(SF) == [], name
    for status, code in ((IncidentStatus.CONTEXT_BUILT, "INCIDENT_NOT_DIAGNOSED"),
                         (IncidentStatus.RESOLVED, "INCIDENT_NOT_DIAGNOSED")):
        SF = base._factory()
        _expect_routing_error(code, routing.ensure_decision, SF, _incident(SF, status=status))
    SF = base._factory()
    _expect_routing_error("NO_AI_REPORT", routing.ensure_decision, SF, _incident(SF, diagnosed_by="RULE_ENGINE"))
    _expect_routing_error("INCIDENT_NOT_FOUND", routing.ensure_decision, SF, uuid.uuid4())
    print(f"✅ PASS: {len(cases)} malformed reports and 4 ineligible incidents are refused before any routing row")


# ─── The proposer enforces the route itself ───

def test_proposer_enforces_route_at_boundary():
    for confidence, status, mode in ((0.8000, pp.VALIDATED, policy.AUTOMATIC_AI),
                                     (0.8001, pp.VALIDATED, policy.AUTOMATIC_AI)):
        SF = base._factory()
        result, llm = _run(SF, _incident(SF, confidence))
        assert result["status"] == status and llm.prompts, (confidence, result["reason_code"])
        assert result["record"]["source_report"]["routing"]["mode"] == mode
        steps = {s["check"]: s["status"] for s in result["record"]["validation"]["steps"]}
        assert steps["confidence_route"] == "PASSED"
    # 0.7999 without an approval: the proposer refuses before the LLM, whoever calls it (CLI, worker).
    SF = base._factory()
    result, llm = _run(SF, _incident(SF, 0.7999))
    assert (result["status"], result["reason_code"]) == (pp.NOT_GENERATED, "LOW_CONFIDENCE") and not llm.prompts
    # A PENDING routing row is not an approval.
    SF = base._factory()
    incident_id = _incident(SF, 0.7999)
    routing.ensure_decision(SF, incident_id)
    result, llm = _run(SF, incident_id)
    assert result["reason_code"] == "LOW_CONFIDENCE" and not llm.prompts
    print("✅ PASS: proposer itself routes 0.8000/0.8001 automatically and refuses 0.7999 without approval")


def test_human_approval_permits_generation_only_after_it_is_recorded():
    SF = base._factory()
    incident_id = _incident(SF, 0.7999)
    dispatcher = Dispatcher(run_inline=_inline_runner(SF))
    _expect_routing_error("APPROVAL_REQUIRED", routing.request_generation, SF, incident_id, dispatcher)
    assert dispatcher.calls == [] and base._rows(SF) == []

    decision, changed = routing.decide(SF, incident_id, policy.APPROVED, {"source": "test"}, "looks right")
    assert changed and decision["decision"] == policy.APPROVED and decision["routing_mode"] == policy.HUMAN_APPROVED
    assert decision["decided_by"]["authenticated"] is False and decision["decided_by"]["identity"] is None
    started = routing.request_generation(SF, incident_id, dispatcher, trigger="approval")
    assert started["started"] and len(dispatcher.calls) == 1

    rows = base._rows(SF)
    assert [r.status for r in rows] == [pp.VALIDATED], [(r.status, r.reason_code) for r in rows]
    routing_info = rows[0].source_report["routing"]
    assert routing_info == {"mode": policy.HUMAN_APPROVED, "approval_required": True,
                            "decision_id": decision["decision_id"], "decided_at": decision["decided_at"]}
    final = _decisions(SF)[0]
    assert (final.proposal_id, final.proposal_status) == (rows[0].id, pp.VALIDATED)
    events = [e["event"] for e in final.audit_log]
    assert events == ["ROUTE_COMPUTED", "APPROVED", "GENERATION_REQUESTED", "GENERATION_FINISHED"], events
    print("✅ PASS: low confidence → refused until APPROVED is recorded → same pipeline → VALIDATED, audited")


def test_human_rejection_prevents_generation():
    SF = base._factory()
    incident_id = _incident(SF, 0.5)
    dispatcher = Dispatcher(run_inline=_inline_runner(SF))
    decision, changed = routing.decide(SF, incident_id, policy.REJECTED, None, "not a capacity problem")
    assert changed and decision["routing_mode"] == policy.HUMAN_REJECTED
    _expect_routing_error("HUMAN_REJECTED", routing.request_generation, SF, incident_id, dispatcher)
    _expect_routing_error("ALREADY_DECIDED", routing.decide, SF, incident_id, policy.APPROVED, None)
    assert routing.decide(SF, incident_id, policy.REJECTED, None)[1] is False   # repeat is a no-op
    result, llm = _run(SF, incident_id)   # the CLI cannot get around it either
    assert result["reason_code"] == "LOW_CONFIDENCE" and not llm.prompts
    assert dispatcher.calls == [] and [r.status for r in base._rows(SF)] == [pp.NOT_GENERATED]
    print("✅ PASS: rejection is final; no generation via API, worker or CLI")


def test_approval_is_bound_to_the_diagnosis_version():
    # Re-diagnosis after approval: the approval does not carry over to the new report.
    SF = base._factory()
    incident_id = _incident(SF, 0.6)
    routing.decide(SF, incident_id, policy.APPROVED, None)
    db = SF()
    db.query(Incident).filter(Incident.id == incident_id).update(
        {"ai_report": base._report(confidence=0.6, root_cause_analysis="A different diagnosis.")})
    db.commit()
    db.close()
    result, llm = _run(SF, incident_id)
    assert result["reason_code"] == "LOW_CONFIDENCE" and not llm.prompts
    assert routing.ensure_decision(SF, incident_id)["decision"] == policy.PENDING   # new version, new decision

    # The approval row is changed during generation: nothing is stored as VALIDATED.
    for change in ({"decision": policy.REJECTED}, {"report_sha256": "0" * 64}, {"route": policy.AUTOMATIC_AI}):
        SF = base._factory()
        incident_id = _incident(SF, 0.6)
        routing.decide(SF, incident_id, policy.APPROVED, None)

        def tamper_then_answer(prompt, change=change):
            db = SF()
            db.query(PatchRoutingDecision).update(change)
            db.commit()
            db.close()
            return base._decision()

        with base.temp_repo() as root:
            result = pp.run_for_incident(SF, incident_id, root, llm=RunnableLambda(tamper_then_answer), env={},
                                         settings=FAST)
        assert (result["status"], result["reason_code"]) == (pp.NOT_GENERATED, "APPROVAL_INVALID"), change
        assert all(r.unified_diff is None for r in base._rows(SF))

    # The incident is rediagnosed during an approved run: INCIDENT_CHANGED (M9 still applies).
    SF = base._factory()
    incident_id = _incident(SF, 0.6)
    routing.decide(SF, incident_id, policy.APPROVED, None)

    def rediagnose_then_answer(prompt):
        db = SF()
        db.query(Incident).filter(Incident.id == incident_id).update({"ai_report": base._report(confidence=0.95)})
        db.commit()
        db.close()
        return base._decision()

    with base.temp_repo() as root:
        result = pp.run_for_incident(SF, incident_id, root, llm=RunnableLambda(rediagnose_then_answer), env={},
                                     settings=FAST)
    assert (result["status"], result["reason_code"]) == (pp.NOT_GENERATED, "INCIDENT_CHANGED")
    print("✅ PASS: approval bound to the report sha256; tampering or re-diagnosis during the run discards the diff")


def test_forged_routing_rows_are_not_approvals():
    forged = [
        {"route": policy.AUTOMATIC_AI, "decision": policy.APPROVED},   # wrong route
        {"route": policy.HUMAN_APPROVAL_REQUIRED, "decision": policy.PENDING},
        {"route": policy.HUMAN_APPROVAL_REQUIRED, "decision": "approved"},
        {"route": policy.HUMAN_APPROVAL_REQUIRED, "decision": policy.APPROVED, "report_sha256": "f" * 64},
    ]
    for fields in forged:
        SF = base._factory()
        incident_id = _incident(SF, 0.6)
        db = SF()
        report = db.query(Incident).filter(Incident.id == incident_id).first().ai_report
        row = {"incident_id": incident_id, "report_sha256": pp.report_sha256(report), "diagnosis_confidence": 0.99,
               "approval_required": False, **fields}
        db.add(PatchRoutingDecision(**row))
        db.commit()
        db.close()
        result, llm = _run(SF, incident_id)
        assert result["reason_code"] == "LOW_CONFIDENCE" and not llm.prompts, fields
    # The routing row's own (possibly stale or forged) confidence is never used: the stored report decides.
    SF = base._factory()
    incident_id = _incident(SF, 0.6)
    routing.ensure_decision(SF, incident_id)
    db = SF()
    db.query(PatchRoutingDecision).update({"diagnosis_confidence": 0.99})
    db.commit()
    db.close()
    result, llm = _run(SF, incident_id)
    assert result["reason_code"] == "LOW_CONFIDENCE" and not llm.prompts
    print(f"✅ PASS: {len(forged) + 1} forged/mismatched routing rows do not authorise generation")


# ─── Idempotency, dispatch failure, kill switch ───

def test_duplicate_requests_and_approvals_start_one_generation():
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    dispatcher = Dispatcher()
    results = [routing.request_generation(SF, incident_id, dispatcher) for _ in range(5)]
    assert [r["started"] for r in results] == [True, False, False, False, False]
    assert len(dispatcher.calls) == 1 and len(_decisions(SF)) == 1

    SF = base._factory()
    incident_id = _incident(SF, 0.5)
    dispatcher = Dispatcher()
    changes = [routing.decide(SF, incident_id, policy.APPROVED, None)[1] for _ in range(3)]
    starts = [routing.request_generation(SF, incident_id, dispatcher, trigger="approval")["started"] for _ in range(3)]
    assert changes == [True, False, False] and starts == [True, False, False] and len(dispatcher.calls) == 1
    approved = [e for e in _decisions(SF)[0].audit_log if e["event"] == "APPROVED"]
    assert len(approved) == 1

    # Two requests that both read "unclaimed": the conditional update lets only one claim it.
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    dispatcher = Dispatcher()
    real_ensure = routing.ensure_decision
    raced = []

    def ensure_then_race(*a, **kw):
        out = real_ensure(*a, **kw)
        if not raced:
            raced.append(True)
            routing.request_generation(SF, incident_id, dispatcher, trigger="concurrent")
        return out

    with patch.object(routing, "ensure_decision", ensure_then_race):
        second = routing.request_generation(SF, incident_id, dispatcher)
    assert second["started"] is False and len(dispatcher.calls) == 1

    # Two approvals that both read PENDING: one transition wins, the other is a no-op.
    SF = base._factory()
    incident_id = _incident(SF, 0.5)
    real_ensure, raced = routing.ensure_decision, []

    def ensure_then_reject(*a, **kw):
        out = real_ensure(*a, **kw)
        if not raced:
            raced.append(True)
            routing.decide(SF, incident_id, policy.REJECTED, None)
        return out

    with patch.object(routing, "ensure_decision", ensure_then_reject):
        _expect_routing_error("ALREADY_DECIDED", routing.decide, SF, incident_id, policy.APPROVED, None)
    assert _decisions(SF)[0].decision == policy.REJECTED
    print("✅ PASS: repeated/concurrent requests and approvals claim generation exactly once")


def test_no_duplicate_generation_after_stale_retry_replay_or_cli():
    # Second review, medium finding: worker down → task A queued; > STALE_AFTER_S later a retry queues
    # task B; when the worker starts, both ran and produced two VALIDATED rows. Now only the current claim runs.
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    dispatcher = Dispatcher()   # records jobs; nothing runs them yet (worker down)
    t0 = pp._utcnow()
    assert routing.request_generation(SF, incident_id, dispatcher, now=t0)["started"]
    later = t0 + datetime.timedelta(seconds=pp.STALE_AFTER_S + 1)
    assert routing.request_generation(SF, incident_id, dispatcher, retry=True, now=later)["started"]
    (_, decision_id, claim_a), (_, _, claim_b) = dispatcher.calls
    assert claim_a != claim_b

    def worker(claim):
        with base.temp_repo() as root:
            return routing.run_generation(SF, incident_id, decision_id, claim, root, llm=base.StubLLM().runnable,
                                          env={}, settings=FAST)

    assert worker(claim_a) == {"started": False, "reason_code": "STALE_CLAIM"}   # superseded job
    assert worker(claim_b)["status"] == pp.VALIDATED                               # current job
    assert worker(claim_b) == {"started": False, "reason_code": "ALREADY_COMPLETED"}   # replay / redelivery
    assert worker(None)["reason_code"] == "STALE_CLAIM"                            # job without a token
    assert [r.status for r in base._rows(SF)] == [pp.VALIDATED]
    events = [e["event"] for e in _decisions(SF)[0].audit_log]
    assert events.count("GENERATION_FINISHED") == 1 and events.count("GENERATION_SKIPPED") == 3

    # Backstop in begin_run: one VALIDATED proposal per diagnosis version on every path (CLI included).
    result, llm = _run(SF, incident_id)
    assert (result["exit_code"], result["status"], result["reason_code"]) == (pp.EXIT_ALREADY_GENERATED, pp.VALIDATED,
                                                                              "ALREADY_GENERATED")
    assert str(result["proposal_id"]) == str(base._rows(SF)[0].id) and not llm.prompts
    assert len(base._rows(SF)) == 1
    from scripts.propose_patch import main as cli_main
    import io
    out = io.StringIO()
    with base.temp_repo() as root:
        assert cli_main([str(incident_id)], session_factory=SF, llm=base.StubLLM().runnable, repo_root=root,
                        env={}, out=out) == 7
    assert "already decided" in out.getvalue() and len(base._rows(SF)) == 1
    # CLI first, then the automatic route: the worker links the existing proposal instead of duplicating it.
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    assert _run(SF, incident_id)[0]["status"] == pp.VALIDATED
    routing.request_generation(SF, incident_id, Dispatcher(run_inline=_inline_runner(SF)))
    row = _decisions(SF)[0]
    assert (row.proposal_status, row.proposal_reason_code) == (pp.VALIDATED, "ALREADY_GENERATED")
    assert row.proposal_id == base._rows(SF)[0].id and len(base._rows(SF)) == 1
    # A new diagnosis version may get its own proposal.
    db = SF()
    db.query(Incident).filter(Incident.id == incident_id).update({"ai_report": base._report(confidence=0.96)})
    db.commit()
    db.close()
    assert _run(SF, incident_id)[0]["status"] == pp.VALIDATED and len(base._rows(SF)) == 2
    print("✅ PASS: superseded/replayed/tokenless jobs skip; one VALIDATED proposal per diagnosis on every path")


def test_one_model_decision_per_version_and_atomic_job_start():
    # Third review, finding 1: a second job with the same claim (replayed/redelivered/forged) started
    # while the first was running, got BUSY, released the live claim, and a fresh generation followed.
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    dispatcher = Dispatcher()
    routing.request_generation(SF, incident_id, dispatcher)
    _, decision_id, claim = dispatcher.calls[0]
    inner = {}

    def runner_that_sees_a_duplicate_job(*a, **kw):
        inner["dup"] = routing.run_generation(SF, incident_id, decision_id, claim, REPO_ROOT,
                                              runner=lambda *x, **y: {"status": pp.FAILED, "reason_code": "BUSY"})
        with base.temp_repo() as root:
            return pp.run_for_incident(SF, incident_id, root, llm=base.StubLLM(base._decision(
                decision="DECLINE", new_value="NONE", decline_reason="LEAK_SUSPECTED")).runnable, env={},
                settings=FAST)

    out = routing.run_generation(SF, incident_id, decision_id, claim, REPO_ROOT,
                                 runner=runner_that_sees_a_duplicate_job)
    assert inner["dup"] == {"started": False, "reason_code": "ALREADY_STARTED"}
    assert (out["status"], out["reason_code"]) == (pp.NOT_GENERATED, "MODEL_DECLINED")
    row = _decisions(SF)[0]
    assert (row.proposal_status, row.proposal_reason_code) == (pp.NOT_GENERATED, "MODEL_DECLINED")
    assert row.generation_requested_at is not None, "the live claim was not released"
    again = Dispatcher()
    assert routing.request_generation(SF, incident_id, again)["started"] is False and again.calls == []
    _expect_routing_error("RETRY_NOT_ALLOWED", routing.request_generation, SF, incident_id, again, retry=True)

    # Finding 2: the CLI could re-roll the model until it validated. Now one model decision per version.
    for first in (base._decision(decision="DECLINE", new_value="NONE", decline_reason="LEAK_SUSPECTED"),
                  base._decision(new_value="999Mi")):   # a model decline, then a REJECTED candidate
        SF = base._factory()
        incident_id = _incident(SF, 0.95)
        result, llm = _run(SF, incident_id, base.StubLLM(first))
        assert result["status"] in (pp.NOT_GENERATED, pp.REJECTED) and llm.prompts
        for _ in range(2):
            result, llm = _run(SF, incident_id)   # would VALIDATE if the model were asked again
            assert (result["exit_code"], result["reason_code"]) == (pp.EXIT_ALREADY_GENERATED, "ALREADY_GENERATED")
            assert not llm.prompts
        llm_calls = []
        runner_llm = lambda: llm_calls.append(1) or base.StubLLM()
        routing.request_generation(SF, incident_id, Dispatcher(run_inline=_inline_runner(SF, runner_llm)))
        assert [r.status for r in base._rows(SF)] in ([pp.NOT_GENERATED], [pp.REJECTED])
        assert _decisions(SF)[0].proposal_reason_code == "ALREADY_GENERATED"

    # Gate-only outcomes do not count as a model decision: LOW_CONFIDENCE before approval, then approved.
    SF = base._factory()
    incident_id = _incident(SF, 0.6)
    assert _run(SF, incident_id)[0]["reason_code"] == "LOW_CONFIDENCE"
    routing.decide(SF, incident_id, policy.APPROVED, None)
    routing.request_generation(SF, incident_id, Dispatcher(run_inline=_inline_runner(SF)))
    assert [r.status for r in base._rows(SF)] == [pp.NOT_GENERATED, pp.VALIDATED]
    print("✅ PASS: duplicate same-claim job skips without releasing the claim; one model decision per version")


def test_retry_rules_and_kill_switch_on_approval():
    # Finding 3: an outcome caused by a transient condition (kill switch flipped after the claim) can be retried.
    SF = base._factory()
    incident_id = _incident(SF, 0.95, system_mode=SystemMode.ACTIVE)
    dispatcher = Dispatcher()
    routing.request_generation(SF, incident_id, dispatcher)
    _, decision_id, claim = dispatcher.calls[0]
    for mode in (SystemMode.DISABLED,):
        db = SF()
        db.query(SystemConfig).update({"system_mode": mode})
        db.commit()
        db.close()
    with base.temp_repo() as root:
        out = routing.run_generation(SF, incident_id, decision_id, claim, root, llm=base.StubLLM().runnable, env={},
                                     settings=FAST)
    assert (out["status"], out["reason_code"]) == (pp.NOT_GENERATED, "SYSTEM_DISABLED")
    db = SF()
    db.query(SystemConfig).update({"system_mode": SystemMode.ACTIVE})
    db.commit()
    db.close()
    client = _client(SF, Dispatcher(run_inline=_inline_runner(SF)))
    view = client.get(f"/api/v1/incidents/{incident_id}/patch-proposals").json()
    assert view["routing"]["retry_allowed"] is True
    assert client.post(f"/api/v1/incidents/{incident_id}/patch-proposals", json={"retry": True}).json()["started"]
    assert [r.status for r in base._rows(SF)] == [pp.NOT_GENERATED, pp.VALIDATED]
    assert client.get(f"/api/v1/incidents/{incident_id}/patch-proposals").json()["routing"]["retry_allowed"] is False

    # Finding 4: a queued job that was lost (worker down) becomes retryable after the stale window - and the
    # view says so, so the dashboard can offer the button.
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    t0 = pp._utcnow() - datetime.timedelta(seconds=pp.STALE_AFTER_S + 5)
    routing.request_generation(SF, incident_id, Dispatcher(), now=t0)
    db = SF()
    view = routing.incident_view(db, incident_id)
    db.close()
    assert view["routing"]["generation_state"] == "QUEUED" and view["routing"]["retry_allowed"] is True
    db = SF()
    fresh = routing.incident_view(db, incident_id, now=t0 + datetime.timedelta(seconds=10))
    db.close()
    assert fresh["routing"]["retry_allowed"] is False, "a recently queued job is not retryable"

    # Finding 5: no approval is recorded while the kill switch is DISABLED; a rejection still is.
    SF = base._factory()
    incident_id = _incident(SF, 0.5, system_mode=SystemMode.DISABLED)
    _expect_routing_error("SYSTEM_DISABLED", routing.decide, SF, incident_id, policy.APPROVED, None)
    assert _decisions(SF) == []
    assert routing.decide(SF, incident_id, policy.REJECTED, None)[0]["decision"] == policy.REJECTED
    print("✅ PASS: transient outcomes and stale queued jobs are retryable; no approvals while DISABLED")


def test_kill_switch_reported_before_config_errors_and_exact_threshold():
    SF = base._factory()
    incident_id = _incident(SF, 0.85, system_mode=SystemMode.DISABLED, project={"confidence_threshold": 0.9})
    dispatcher = Dispatcher()
    with patch.dict(os.environ, {"PATCH_PROPOSAL_MIN_CONFIDENCE": "0.95"}):
        _expect_routing_error("SYSTEM_DISABLED", routing.request_generation, SF, incident_id, dispatcher)
        response = _client(SF, dispatcher).post(f"/api/v1/incidents/{incident_id}/patch-proposals")
        assert response.status_code == 409 and response.json()["detail"].startswith("SYSTEM_DISABLED")
    assert dispatcher.calls == []
    # No tolerance: a threshold a hair above 0.80 is a conflict, not "close enough".
    for value in ("0.8000000005", "0.80000000000001"):
        try:
            pp.settings_from_env({"PATCH_PROPOSAL_MIN_CONFIDENCE": value})
            raise AssertionError(value)
        except ValueError:
            pass
    assert pp.settings_from_env({"PATCH_PROPOSAL_MIN_CONFIDENCE": "0.800"}).min_confidence == 0.8
    # Even a Settings object forced past validation cannot move the generation threshold.
    forced = pp.Settings(llm_timeout_s=5.0, guard_margin_s=5.0)
    object.__setattr__(forced, "min_confidence", 0.95)
    SF = base._factory()
    with base.temp_repo() as root:
        result = pp.run_for_incident(SF, _incident(SF, 0.85), root, llm=base.StubLLM().runnable, env={},
                                     settings=forced)
    assert result["status"] == pp.VALIDATED
    print("✅ PASS: kill switch reported first on the API path; threshold equality is exact")


def test_dispatch_failure_and_kill_switch():
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    _expect_routing_error("DISPATCH_FAILED", routing.request_generation, SF, incident_id, Dispatcher(fail=True))
    row = _decisions(SF)[0]
    assert row.generation_requested_at is None and row.audit_log[-1]["event"] == "GENERATION_RELEASED"
    dispatcher = Dispatcher()
    assert routing.request_generation(SF, incident_id, dispatcher)["started"] and len(dispatcher.calls) == 1

    SF = base._factory()
    incident_id = _incident(SF, 0.95, system_mode=SystemMode.DISABLED)
    dispatcher = Dispatcher()
    _expect_routing_error("SYSTEM_DISABLED", routing.request_generation, SF, incident_id, dispatcher)
    # The kill switch is checked first: no routing row is recorded and nothing is claimed or queued.
    assert dispatcher.calls == [] and _decisions(SF) == []
    print("✅ PASS: a failed dispatch releases the claim; DISABLED kill switch starts nothing")


def test_run_generation_rechecks_and_links():
    # Worker receives a job for a decision that is not authorised (e.g. forged job): it does not run.
    SF = base._factory()
    incident_id = _incident(SF, 0.5)
    decision = routing.ensure_decision(SF, incident_id)
    called = []
    for claim in (None, "2026-01-01T00:00:00Z"):   # a PENDING decision was never claimed
        out = routing.run_generation(SF, incident_id, decision["decision_id"], claim, REPO_ROOT,
                                     runner=lambda *a, **k: called.append(1))
        assert out == {"started": False, "reason_code": "STALE_CLAIM"} and not called

    # The diagnosis changed between dispatch and execution: the old decision is superseded.
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    dispatcher = Dispatcher()
    routing.request_generation(SF, incident_id, dispatcher)
    _, decision_id, claim = dispatcher.calls[0]
    db = SF()
    db.query(Incident).filter(Incident.id == incident_id).update({"ai_report": base._report(confidence=0.97)})
    db.commit()
    db.close()
    out = routing.run_generation(SF, incident_id, decision_id, claim, REPO_ROOT,
                                 runner=lambda *a, **k: called.append(1))
    assert out["reason_code"] == "DIAGNOSIS_SUPERSEDED" and not called

    # A real automatic run is linked to its decision with the outcome; a REJECTED outcome is recorded as such.
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    bad = lambda: base.StubLLM(base._decision(new_value="999Mi"))
    routing.request_generation(SF, incident_id, Dispatcher(run_inline=_inline_runner(SF, bad)))
    row = _decisions(SF)[0]
    assert (row.proposal_status, row.proposal_reason_code) == (pp.REJECTED, "LLM_INVALID_CHOICE")
    assert row.proposal_id == base._rows(SF)[0].id
    # REJECTED is a deterministic outcome: no retry; FAILED can be retried.
    _expect_routing_error("RETRY_NOT_ALLOWED", routing.request_generation, SF, incident_id, Dispatcher(), retry=True)
    db = SF()
    db.query(PatchRoutingDecision).update({"proposal_status": pp.FAILED})
    db.commit()
    db.close()
    dispatcher = Dispatcher()
    assert routing.request_generation(SF, incident_id, dispatcher, retry=True)["started"] and dispatcher.calls

    # A runner crash is recorded as FAILED on the decision, never as success.
    SF = base._factory()
    incident_id = _incident(SF, 0.95)
    dispatcher = Dispatcher()
    routing.request_generation(SF, incident_id, dispatcher)
    _, decision_id, claim = dispatcher.calls[0]

    def crash(*a, **k):
        raise RuntimeError("boom\n[SQL: SELECT secret]")

    out = routing.run_generation(SF, incident_id, decision_id, claim, REPO_ROOT, runner=crash)
    row = _decisions(SF)[0]
    assert (out["status"], row.proposal_status, row.proposal_reason_code) == (pp.FAILED, pp.FAILED, "INTERNAL_ERROR")
    assert "SELECT" not in str(row.audit_log)
    print("✅ PASS: worker re-checks authorisation and diagnosis version; outcomes linked and audited")


# ─── API (the dashboard's only path to the backend) ───

def test_api_high_confidence_starts_automatically_once():
    SF = base._factory()
    incident_id = _incident(SF, 0.8)
    dispatcher = Dispatcher(run_inline=_inline_runner(SF))
    client = _client(SF, dispatcher)
    view = client.get(f"/api/v1/incidents/{incident_id}/patch-proposals").json()
    assert view["routing"]["routing_mode"] == policy.AUTOMATIC_AI and view["routing"]["recorded"] is False
    assert view["routing"]["generation_state"] == "NOT_REQUESTED" and view["latest_proposal"] is None
    assert view["applied"] is False and view["deployment_available"] is False
    assert _decisions(SF) == [], "GET must not write"

    first = client.post(f"/api/v1/incidents/{incident_id}/patch-proposals")
    assert first.status_code == 202 and first.json()["started"] is True
    for _ in range(3):   # page refreshes / repeated clicks
        again = client.post(f"/api/v1/incidents/{incident_id}/patch-proposals", json={})
        assert again.status_code == 202 and again.json()["started"] is False
    assert len(dispatcher.calls) == 1 and len(base._rows(SF)) == 1

    view = client.get(f"/api/v1/incidents/{incident_id}/patch-proposals").json()
    latest = view["latest_proposal"]
    assert view["routing"]["generation_state"] == "COMPLETED"
    assert latest["effective_status"] == pp.VALIDATED and latest["matches_current_diagnosis"] is True
    assert latest["source_report"]["routing"]["mode"] == policy.AUTOMATIC_AI
    assert latest["applied"] is False and latest["stage"] == "PROPOSAL_ONLY"
    pid = latest["id"]
    diff = client.get(f"/api/v1/patch-proposals/{pid}/diff").json()
    assert diff["available"] and diff["unified_diff"] == base._rows(SF)[0].unified_diff and diff["applied"] is False
    validation = client.get(f"/api/v1/patch-proposals/{pid}/validation").json()
    assert {s["check"] for s in validation["steps"]} >= set(pp.MANDATORY_CHECKS)
    assert client.get(f"/api/v1/patch-proposals/{pid}").json()["unified_diff"] == diff["unified_diff"]
    assert client.post(f"/api/v1/incidents/{incident_id}/patch-proposals/approve").status_code == 409
    print("✅ PASS: API: ≥80% starts once (refresh-safe), real stored diff served, approval not applicable")


def test_api_low_confidence_requires_approval_and_ignores_client_confidence():
    SF = base._factory()
    incident_id = _incident(SF, 0.7999)
    dispatcher = Dispatcher(run_inline=_inline_runner(SF))
    client = _client(SF, dispatcher)
    url = f"/api/v1/incidents/{incident_id}/patch-proposals"

    view = client.get(url).json()
    assert view["routing"]["routing_mode"] == policy.HUMAN_APPROVAL_REQUIRED
    assert view["routing"]["generation_state"] == "AWAITING_APPROVAL"
    blocked = client.post(url)
    assert blocked.status_code == 403 and blocked.json()["detail"].startswith("APPROVAL_REQUIRED")
    # Client-supplied confidence/route/approval fields are refused, not trusted.
    for body in ({"confidence": 0.99}, {"route": "AUTOMATIC_AI"}, {"approved": True}, {"retry": True, "confidence": 1}):
        assert client.post(url, json=body).status_code == 422, body
        assert client.post(url + "/approve", json=body).status_code == 422, body
    assert dispatcher.calls == [] and base._rows(SF) == []
    assert _decisions(SF)[0].decision == policy.PENDING

    approved = client.post(url + "/approve", json={"reason": "telemetry matches"},
                           headers={"user-agent": "drill-browser"})
    assert approved.status_code == 200 and approved.json()["started"] is True
    again = client.post(url + "/approve")
    assert again.status_code == 200 and again.json()["changed"] is False and again.json()["started"] is False
    assert len(dispatcher.calls) == 1
    assert client.post(url + "/reject").status_code == 409

    view = client.get(url).json()
    assert view["routing"]["routing_mode"] == policy.HUMAN_APPROVED
    who = view["routing"]["decided_by"]
    assert who["authenticated"] is False and who["identity"] is None and who["user_agent"] == "drill-browser"
    assert view["latest_proposal"]["effective_status"] == pp.VALIDATED
    assert view["latest_proposal"]["source_report"]["routing"]["mode"] == policy.HUMAN_APPROVED
    print("✅ PASS: API: <80% blocked until approval; client confidence/route rejected; approval idempotent")


def test_api_rejection_failure_and_errors():
    SF = base._factory()
    incident_id = _incident(SF, 0.3)
    dispatcher = Dispatcher()
    client = _client(SF, dispatcher)
    url = f"/api/v1/incidents/{incident_id}/patch-proposals"
    rejected = client.post(url + "/reject", json={"reason": "leak, not capacity"})
    assert rejected.status_code == 200 and rejected.json()["view"]["routing"]["routing_mode"] == policy.HUMAN_REJECTED
    assert client.post(url).json()["detail"].startswith("HUMAN_REJECTED")
    assert client.post(url + "/approve").status_code == 409
    view = client.get(url).json()
    assert view["routing"]["generation_state"] == "REJECTED_BY_HUMAN" and view["routing"]["decision_reason"]
    assert dispatcher.calls == []

    # A validation REJECTED proposal: reason shown, no diff served.
    SF = base._factory()
    incident_id = _incident(SF, 0.9)
    bad = lambda: base.StubLLM(base._decision(new_value="NONE", decision="PROPOSE"))
    client = _client(SF, Dispatcher(run_inline=_inline_runner(SF, bad)))
    client.post(f"/api/v1/incidents/{incident_id}/patch-proposals")
    latest = client.get(f"/api/v1/incidents/{incident_id}/patch-proposals").json()["latest_proposal"]
    assert latest["effective_status"] == pp.REJECTED and latest["effective_reason_code"] == "LLM_INVALID_CHOICE"
    assert latest["unified_diff"] is None and latest["reason_detail"]
    diff = client.get(f"/api/v1/patch-proposals/{latest['id']}/diff").json()
    assert diff["available"] is False and diff["unified_diff"] is None

    # A FAILED proposal (LLM unreachable): clear reason, retry allowed.
    SF = base._factory()
    incident_id = _incident(SF, 0.9)
    down = lambda: base.StubLLM(error=ConnectionError("refused"))
    client = _client(SF, Dispatcher(run_inline=_inline_runner(SF, down)))
    client.post(f"/api/v1/incidents/{incident_id}/patch-proposals")
    latest = client.get(f"/api/v1/incidents/{incident_id}/patch-proposals").json()["latest_proposal"]
    assert (latest["effective_status"], latest["effective_reason_code"]) == (pp.FAILED, "LLM_UNAVAILABLE")
    assert client.post(f"/api/v1/incidents/{incident_id}/patch-proposals", json={"retry": True}).json()["started"]

    # Errors: unknown ids, bad ids, ineligible incidents, invalid confidence, broker down.
    SF = base._factory()
    client = _client(SF, Dispatcher(fail=True))
    assert client.get(f"/api/v1/incidents/{uuid.uuid4()}/patch-proposals").status_code == 404
    assert client.get(f"/api/v1/patch-proposals/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/v1/patch-proposals/not-a-uuid").status_code == 422
    assert client.post("/api/v1/incidents/x/patch-proposals").status_code == 422
    building = _incident(SF, 0.9, status=IncidentStatus.CONTEXT_BUILT)
    assert client.post(f"/api/v1/incidents/{building}/patch-proposals").status_code == 409
    view = client.get(f"/api/v1/incidents/{building}/patch-proposals").json()
    assert view["routing"] is None and view["routing_error"]["code"] == "INCIDENT_NOT_DIAGNOSED"
    nan_incident = _incident(SF, report=base._report(confidence=float("nan")))
    assert client.post(f"/api/v1/incidents/{nan_incident}/patch-proposals").status_code == 422
    healthy = _incident(SF, 0.9)
    down = client.post(f"/api/v1/incidents/{healthy}/patch-proposals")
    assert down.status_code == 503 and down.json()["detail"].startswith("DISPATCH_FAILED")
    print("✅ PASS: API: human rejection, REJECTED/FAILED proposals with reasons, and error codes")


def test_api_refuses_cross_site_simple_requests():
    # A cross-site page can send these without a CORS preflight (no-cors fetch, HTML form).
    SF = base._factory()
    incident_id = _incident(SF, 0.5)
    high = _incident(SF, 0.95)
    dispatcher = Dispatcher()
    client = _client(SF, dispatcher)
    simple = [dict(content=b""), dict(content=b"", headers={"content-type": "text/plain"}),
              dict(content=b"{}", headers={"content-type": "text/plain;charset=UTF-8"}),
              dict(data={"reason": "x"}), dict(content=b"reason=x", headers={"content-type": "multipart/form-data"})]
    for kw in simple:
        for path in (f"{incident_id}/patch-proposals/approve", f"{incident_id}/patch-proposals/reject",
                     f"{high}/patch-proposals"):
            response = client.post(f"/api/v1/incidents/{path}", **kw)
            assert response.status_code == 415, (path, kw, response.status_code)
    assert dispatcher.calls == [] and _decisions(SF) == [] and base._rows(SF) == []
    # The dashboard's JSON requests still work.
    assert client.post(f"/api/v1/incidents/{incident_id}/patch-proposals/approve",
                       json={"reason": None}).status_code == 200
    assert client.post(f"/api/v1/incidents/{high}/patch-proposals",
                       headers={"content-type": "application/json; charset=utf-8"}, content=b"{}").status_code == 202

    # The view reports whether automatic generation is on, so the page can honour PATCH_PROPOSALS_AUTO.
    for value, expected in (("true", True), ("false", False), ("", False)):
        with patch.dict(os.environ, {"PATCH_PROPOSALS_AUTO": value}):
            assert client.get(f"/api/v1/incidents/{high}/patch-proposals").json()["auto_generation_enabled"] is expected
    print("✅ PASS: approve/reject/start refuse non-JSON (cross-site simple) requests with 415; JSON still works")


def test_finalize_rederives_route_from_stored_report():
    # Defence in depth: a VALIDATED record for a <0.80 report whose in-memory routing says AUTOMATIC_AI
    # (or anything else) is not stored unless a matching APPROVED row exists in the database.
    for forged_mode in (policy.AUTOMATIC_AI, None, "HUMAN_APPROVED"):
        SF = base._factory()
        incident_id = _incident(SF, 0.5)
        db = SF()
        snap = pp.snapshot_incident(db.query(Incident).filter(Incident.id == incident_id).first())
        db.close()
        fake_approval = {"decision_id": str(uuid.uuid4()), "route": policy.HUMAN_APPROVAL_REQUIRED,
                         "decision": policy.APPROVED, "report_sha256": pp.report_sha256(snap["ai_report"])}
        with base.temp_repo() as root:
            rec = pp.propose(snap, {}, "ACTIVE", root, llm=base.StubLLM().runnable, settings=FAST, env={},
                             approval=fake_approval)
        assert rec["status"] == pp.VALIDATED   # in memory only: propose() was handed a forged approval
        rec["source_report"]["routing"]["mode"] = forged_mode
        proposal_id = pp.begin_run(SF, incident_id, None)
        assert pp.finalize(SF, proposal_id, rec, snap=snap) is True
        row = base._rows(SF)[0]
        assert (row.status, row.reason_code, row.unified_diff) == (pp.NOT_GENERATED, "APPROVAL_INVALID", None), forged_mode
    print("✅ PASS: finalize re-derives the route from the stored report; forged in-memory routing is not stored")


def test_worker_hook_after_diagnosis():
    from workers import tasks

    for confidence, auto_env, expect_dispatch, expect_rows in ((0.95, "true", 1, 1), (0.5, "true", 0, 1),
                                                               (0.95, "false", 0, 0)):
        SF = base._factory()
        incident_id = _incident(SF, confidence)
        with patch.object(tasks, "SessionLocal", SF), \
             patch.object(tasks.generate_patch_proposal, "apply_async") as apply_async, \
             patch.dict(os.environ, {"PATCH_PROPOSALS_AUTO": auto_env}):
            tasks._route_patch_proposal(str(incident_id))
            tasks._route_patch_proposal(str(incident_id))   # a retried diagnosis task
        assert apply_async.call_count == expect_dispatch, (confidence, auto_env)
        assert len(_decisions(SF)) == expect_rows
        if expect_rows and confidence < 0.8:
            assert _decisions(SF)[0].decision == policy.PENDING
    # A broken database never breaks the diagnosis task.
    with patch.object(tasks, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down"))):
        tasks._route_patch_proposal(str(uuid.uuid4()))
    print("✅ PASS: post-diagnosis hook queues once for ≥80%, records PENDING for <80%, can be disabled")


# ─── No-apply boundary ───

def test_no_apply_or_deploy_path():
    from api.main import app as real_app
    client = TestClient(real_app)
    paths = client.get("/openapi.json").json()["paths"]
    stage2 = {p: v for p, v in paths.items() if "patch-proposals" in p}
    assert len(stage2) == 6
    for path in stage2:
        assert not any(word in path.lower() for word in ("apply", "deploy", "merge", "commit", "push", "execute")), path

    forbidden = ("engine.remediation", "engine.target_resolver", "engine.policy_engine", "github", "kubernetes",
                 "subprocess")
    for rel in ("engine/patch_routing.py", "engine/routing_policy.py", "api/patch_proposals.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for mod in mods:
                assert not any(mod == f or mod.startswith(f + ".") for f in forbidden), (rel, mod)
                if rel.startswith("engine/"):
                    assert not mod.startswith("workers"), (rel, mod)
                else:
                    assert mod in ("", "workers.tasks") or not mod.startswith("workers"), (rel, mod)

    # A full approved run touches neither the incident status nor the real manifest.
    manifest = (REPO_ROOT / base.REL).read_bytes()
    mtime = os.stat(REPO_ROOT / base.REL).st_mtime_ns
    SF = base._factory()
    incident_id = _incident(SF, 0.6, report=base.real_manifest_report(confidence=0.6))
    run_real = lambda i, d, c: routing.run_generation(SF, i, d, c, REPO_ROOT, llm=base.MenuStubLLM().runnable, env={},
                                                      settings=FAST)
    client = _client(SF, Dispatcher(run_inline=run_real))
    assert client.post(f"/api/v1/incidents/{incident_id}/patch-proposals/approve").json()["started"]
    assert base._rows(SF)[0].status == pp.VALIDATED
    assert (REPO_ROOT / base.REL).read_bytes() == manifest and os.stat(REPO_ROOT / base.REL).st_mtime_ns == mtime
    db = SF()
    assert db.query(Incident).filter(Incident.id == incident_id).first().status == IncidentStatus.DIAGNOSED
    db.close()
    print("✅ PASS: no apply/deploy endpoint; no remediation/GitHub imports; real manifest and incident untouched")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("AI DRILL: Confidence Routing & Human Approval (Stage 2)")
    print("=" * 60 + "\n")
    test_routing_threshold_boundaries()
    test_one_threshold_conflicting_configuration_refused()
    test_malformed_reports_cannot_be_routed()
    test_proposer_enforces_route_at_boundary()
    test_human_approval_permits_generation_only_after_it_is_recorded()
    test_human_rejection_prevents_generation()
    test_approval_is_bound_to_the_diagnosis_version()
    test_forged_routing_rows_are_not_approvals()
    test_duplicate_requests_and_approvals_start_one_generation()
    test_no_duplicate_generation_after_stale_retry_replay_or_cli()
    test_one_model_decision_per_version_and_atomic_job_start()
    test_retry_rules_and_kill_switch_on_approval()
    test_kill_switch_reported_before_config_errors_and_exact_threshold()
    test_dispatch_failure_and_kill_switch()
    test_run_generation_rechecks_and_links()
    test_api_high_confidence_starts_automatically_once()
    test_api_low_confidence_requires_approval_and_ignores_client_confidence()
    test_api_rejection_failure_and_errors()
    test_api_refuses_cross_site_simple_requests()
    test_finalize_rederives_route_from_stored_report()
    test_worker_hook_after_diagnosis()
    test_no_apply_or_deploy_path()
    print("\n🎯 ALL CHECKS PASSED\n")
