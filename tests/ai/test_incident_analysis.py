"""
AI Drill: Incident Analysis Stage
==================================
diagnose_incident (AI_INCIDENT_ANALYSIS=true) must:
  context + curated runbook RAG → analyst LLM → structured AI report → DIAGNOSED → STOP

Offline only: SQLite, a stub LLM that records its prompt, and a real in-memory
Qdrant seeded with the curated runbooks (cached embedding model, HF_HUB_OFFLINE).
No Docker, Postgres, Redis, OpenAI, GitHub or network access.
"""
import os
import re
import sys
import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from langchain_core.runnables import RunnableLambda
from qdrant_client import QdrantClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.database import Base
from api.models import Incident, IncidentStatus
from engine.ai_diagnostics import (
    AIDiagnosisEngine, AIIncidentReport, AIReportValidationError, validate_report,
)
from engine.memory import QdrantMemoryStore
from engine.runbooks import load_runbooks
import workers.tasks as tasks

_SEEDED_STORE = None


def _seeded_store():
    global _SEEDED_STORE
    if _SEEDED_STORE is None:
        _SEEDED_STORE = QdrantMemoryStore(client=QdrantClient(location=":memory:"))
        _SEEDED_STORE.upsert_runbooks(load_runbooks())
    return _SEEDED_STORE


def _empty_store():
    return QdrantMemoryStore(client=QdrantClient(location=":memory:"))


def _make_session_factory():
    db_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(db_engine)
    return sessionmaker(bind=db_engine, autocommit=False, autoflush=False)


def _create_chaos_crash_incident(Session):
    db = Session()
    incident = Incident(
        alert_fingerprint="drill", alert_name="ChaosInjection:CrashLoop", severity="HIGH",
        status=IncidentStatus.CONTEXT_BUILT,
        raw_payload_cache={
            "chaos": True, "fault_type": "crash_loop", "target_url": "http://localhost:8001",
            "labels": {"alertname": "ChaosInjection:CrashLoop", "namespace": "autofixops", "severity": "high"},
            "annotations": {"summary": "Target app process keeps restarting"},
        },
    )
    db.add(incident)
    db.commit()
    incident_id = incident.id  # uuid.UUID: SQLite's UUID type can't bind str ids
    db.close()
    return incident_id


def _load(Session, incident_id):
    db = Session()
    i = db.query(Incident).filter(Incident.id == incident_id).first()
    snapshot = {
        "status": i.status, "ai_report": i.ai_report, "payload": i.raw_payload_cache,
        "timeline": [t["status"] for t in (i.status_timeline or [])],
        "classification": i.diagnosis_classification, "confidence": i.diagnosis_confidence,
        "reasoning": i.diagnosis_reasoning, "diagnosed_by": i.diagnosed_by,
    }
    db.close()
    return snapshot


def _report_json(cited_ids, root_cause="Repeated process restarts point to a crash at startup; cause uncertain.",
                 guidance="Runbook guidance: read logs from the crashed instance and classify the exit."):
    return json.dumps({
        "incident_summary": "The target application is crash-looping after chaos fault injection.",
        "incident_type": "CRASH_LOOP",
        "observed_evidence": ["Alert ChaosInjection:CrashLoop fired with severity HIGH",
                              "Chaos fault_type is crash_loop"],
        "retrieved_runbook_evidence": [{"runbook_id": rid, "guidance_used": guidance} for rid in cited_ids],
        "root_cause_analysis": root_cause,
        "reasoning": ["The fault was deliberately injected", "No telemetry contradicts a crash loop"],
        "recommended_remediation": "Inspect crash logs; consider a rollback only if a release caused it.",
        "proposed_action": {"type": "ESCALATE", "target": "autofixops-target-app",
                            "rationale": "Cause not confirmed from evidence."},
        "confidence": 0.62,
        "risk_impact": {"level": "HIGH", "description": "Service is intermittently unavailable."},
    })


class StubLLM:
    """Records every prompt; answers by citing exactly the runbook ids the prompt offered."""

    def __init__(self, respond=None, error=None):
        self.prompts = []
        self.error = error
        self.respond = respond or (lambda prompt: _report_json(re.findall(r"runbook_id: (\S+)", prompt)))
        self.runnable = RunnableLambda(self._call)

    def _call(self, prompt_value):
        prompt = prompt_value.to_string()
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.respond(prompt)


class _ContextCollection:
    def __init__(self, doc):
        self.doc = doc

    def find_one(self, *a, **kw):
        return self.doc


TELEMETRY_DOC = {
    "metrics": {"cpu": [{"value": [1, "0.31"]}], "memory": [{"value": [1, "0.47"]}]},
    "target_pod": "autofixops-target-app",
}


def _run_diagnosis(Session, incident_id, llm, store, retries=0, events=None, env=None):
    """Runs diagnose_incident with stubs; returns the remediation guards for assertions."""
    guards = {name: MagicMock(name=name) for name in (
        "evaluate_policy", "execute_remediation", "verify_resolution",
        "RemediationEngine", "PolicyDecisionEngine", "PatchGenerator", "TargetResolver",
    )}
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env or {"AI_INCIDENT_ANALYSIS": "true"}))
        stack.enter_context(patch.object(tasks, "SessionLocal", Session))
        stack.enter_context(patch.object(tasks, "_check_kill_switch", lambda _id: False))
        stack.enter_context(patch.object(
            tasks, "emit_sync", lambda t, d, i=None: events.append((t, d)) if events is not None else None))
        stack.enter_context(patch.object(tasks, "incident_contexts_collection", _ContextCollection(TELEMETRY_DOC)))
        stack.enter_context(patch.object(tasks, "QdrantMemoryStore", lambda: store))
        stack.enter_context(patch.object(tasks, "AIDiagnosisEngine", lambda: AIDiagnosisEngine(llm=llm.runnable)))
        for task_name in ("evaluate_policy", "execute_remediation", "verify_resolution"):
            stack.enter_context(patch.object(getattr(tasks, task_name), "apply_async", guards[task_name]))
        for cls in ("RemediationEngine", "PolicyDecisionEngine", "TargetResolver"):
            stack.enter_context(patch.object(tasks, cls, guards[cls]))
        stack.enter_context(patch("engine.patch_generator.PatchGenerator.generate", guards["PatchGenerator"]))

        task = tasks.diagnose_incident
        task.push_request(retries=retries)
        try:
            task.run(incident_id)
        finally:
            task.pop_request()
    return guards


def _assert_no_remediation(guards, allow=()):
    for name, guard in guards.items():
        if name not in allow:
            assert not guard.called, f"{name} was called during the analysis stage"


# ─── Tests ───

def test_context_reaches_ai_layer():
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    llm = StubLLM()
    guards = _run_diagnosis(Session, incident_id, llm, _seeded_store())
    prompt = llm.prompts[0]
    for expected in ("ChaosInjection:CrashLoop", '"fault_type": "crash_loop"',
                     "Target app process keeps restarting", '"namespace": "autofixops"',
                     '"available": true', '"cpu_peak": 0.31', "UNKNOWN_ANOMALY"):
        assert expected in prompt, f"missing from prompt: {expected}"
    _assert_no_remediation(guards)
    print("✅ PASS: Alert, labels, annotations, chaos fault, telemetry and rule output reach the LLM")


def test_relevant_runbooks_retrieved_and_passed_to_llm():
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    llm = StubLLM()
    guards = _run_diagnosis(Session, incident_id, llm, _seeded_store())
    rag = _load(Session, incident_id)["ai_report"]["rag"]
    assert rag["status"] == "ok" and rag["collection"] == "operational_runbooks"
    assert rag["retrieved"][0]["runbook_id"] == "k8s-repeated-container-failure", rag["retrieved"]
    assert rag["retrieved"][0]["source_organization"] == "Kubernetes"
    assert all(h["similarity_score"] >= rag["min_score"] for h in rag["retrieved"])

    prompt = llm.prompts[0]
    assert "runbook_id: k8s-repeated-container-failure" in prompt
    assert ("source: Kubernetes — Debug Running Pods "
            "(https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/)") in prompt
    assert "source_type: official_documentation | source_status: curated | verified: True" in prompt
    assert "kubectl logs <pod> --previous" in prompt  # document content
    assert "OFFICIAL OPERATIONAL GUIDANCE curated from Kubernetes" in prompt
    assert "NOT historical incidents" in prompt
    assert "do NOT prove anything about the current incident" in prompt
    assert "Never invent Kubernetes events" in prompt and "source URLs" in prompt
    _assert_no_remediation(guards)
    print(f"✅ PASS: Runbooks retrieved {[(h['incident_type'], h['similarity_score']) for h in rag['retrieved']]} "
          f"and passed to the LLM as official guidance")


def test_structured_report_persisted_and_pipeline_stops():
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    events = []
    guards = _run_diagnosis(Session, incident_id, StubLLM(), _seeded_store(), events=events)
    snap = _load(Session, incident_id)
    report = snap["ai_report"]

    assert snap["status"] == IncidentStatus.DIAGNOSED
    assert snap["timeline"] == ["DIAGNOSED"]
    for field in ("incident_summary", "observed_evidence", "retrieved_runbook_evidence",
                  "root_cause_analysis", "reasoning", "recommended_remediation",
                  "proposed_action", "confidence", "risk_impact"):
        assert field in report, f"report missing {field}"
    assert report["role"] == "ANALYST_ONLY"
    assert report["requires_human_approval"] is True and report["proposed_action_executed"] is False
    assert report["evidence_sources"]["observed_evidence"] == "incident_context"
    assert report["evidence_sources"]["retrieved_runbook_evidence"] == "curated_official_documentation"
    assert report["evidence_sources"]["root_cause_analysis"] == "llm_inference"
    assert snap["classification"] == "CRASH_LOOP" and snap["confidence"] == 0.62
    assert snap["diagnosed_by"] == "AI_ENGINE" and snap["reasoning"] == report["root_cause_analysis"]
    assert [t for t, _ in events] == ["incident.diagnosed", "incident.status_changed", "incident.ai_report_ready"]
    _assert_no_remediation(guards)
    print("✅ PASS: Structured report persisted; incident stops at DIAGNOSED; no policy/remediation dispatched")


def test_runbook_citations_labelled_official():
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    _run_diagnosis(Session, incident_id, StubLLM(), _seeded_store())
    citations = _load(Session, incident_id)["ai_report"]["retrieved_runbook_evidence"]
    official = {d["runbook_id"]: d for d in load_runbooks()}
    assert citations, "expected at least one cited runbook"
    for c in citations:
        stored = official[c["runbook_id"]]
        assert c["source_type"] == "official_documentation"
        assert c["source_status"] == "curated"
        assert c["verified"] is True
        assert "not a historical incident" in c["disclaimer"]
        assert isinstance(c["similarity_score"], float)
        # Source metadata comes from the store, never from the LLM output.
        for field in ("source_organization", "source_title", "source_url"):
            assert c[field] == stored[field], field
    print("✅ PASS: Every cited document carries official/curated/verified labels and its real source URL")


def test_historical_claims_rejected():
    hits = [{"runbook_id": "k8s-repeated-container-failure"}]

    def report(**overrides):
        return AIIncidentReport.model_validate_json(_report_json(["k8s-repeated-container-failure"], **overrides))

    validate_report(report(), hits)  # clean report passes
    for claim in ("This runbook describes a previous incident that was resolved by rollback.",
                  "Historically, a rollback fixed this.",
                  "The same fix was successfully resolved in the runbook case.",
                  "A rollback has worked before for this alert."):
        try:
            validate_report(report(guidance=claim), hits)
            assert False, f"historical claim accepted: {claim}"
        except AIReportValidationError:
            pass
    try:
        validate_report(AIIncidentReport.model_validate_json(_report_json(["runbook-invented"])), hits)
        assert False, "fabricated runbook id accepted"
    except AIReportValidationError as e:
        assert "runbook-invented" in str(e)
    print("✅ PASS: Historical-incident claims and fabricated runbook ids are rejected")


def test_historical_claim_in_pipeline_fails_incident():
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    llm = StubLLM(respond=lambda prompt: _report_json(
        ["k8s-repeated-container-failure"], guidance="This matches a past incident where rollback successfully resolved it."))
    guards = _run_diagnosis(Session, incident_id, llm, _seeded_store(), retries=tasks.diagnose_incident.max_retries)
    snap = _load(Session, incident_id)
    assert snap["status"] == IncidentStatus.FAILED
    assert snap["ai_report"] is None
    assert "historical claim" in snap["payload"]["pipeline_failure"]["error"]
    _assert_no_remediation(guards)
    print("✅ PASS: A report claiming runbook history is never stored; incident ends FAILED")


def test_no_relevant_runbook_is_explicit():
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    llm = StubLLM()
    _run_diagnosis(Session, incident_id, llm, _empty_store())
    report = _load(Session, incident_id)["ai_report"]
    assert "No relevant runbook was found" in llm.prompts[0]
    assert "runbook_id:" not in llm.prompts[0]
    assert report["rag"]["status"] == "empty_collection" and report["rag"]["retrieved"] == []
    assert report["retrieved_runbook_evidence"] == []
    print("✅ PASS: With no runbooks the LLM is told so explicitly and no evidence is fabricated")


def test_llm_failure_marks_failed_with_original_error():
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    llm = StubLLM(error=RuntimeError("Error code: 401 - Incorrect API key provided"))
    guards = _run_diagnosis(Session, incident_id, llm, _seeded_store(), retries=tasks.diagnose_incident.max_retries)
    snap = _load(Session, incident_id)
    assert snap["status"] == IncidentStatus.FAILED
    assert snap["timeline"] == ["FAILED"]
    assert "Incorrect API key provided" in snap["payload"]["pipeline_failure"]["error"]
    assert snap["payload"]["pipeline_failure"]["stage"] == "diagnose_incident"
    _assert_no_remediation(guards)
    print("✅ PASS: LLM call failure on the last retry → FAILED with the original error preserved")


def test_missing_api_key_marks_failed():
    """Real ChatOpenAI construction with no key (no network call is made)."""
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    env["AI_INCIDENT_ANALYSIS"] = "true"
    env["LLM_PROVIDER"] = "openai"
    with patch.dict(os.environ, env, clear=True), \
         patch.object(tasks, "SessionLocal", Session), \
         patch.object(tasks, "_check_kill_switch", lambda _id: False), \
         patch.object(tasks, "emit_sync", lambda *a, **kw: None), \
         patch.object(tasks, "QdrantMemoryStore", _seeded_store), \
         patch.object(tasks.evaluate_policy, "apply_async") as policy_dispatch:
        task = tasks.diagnose_incident
        task.push_request(retries=task.max_retries)
        try:
            task.run(incident_id)
        finally:
            task.pop_request()
    snap = _load(Session, incident_id)
    assert snap["status"] == IncidentStatus.FAILED, snap["status"]
    assert "api_key" in snap["payload"]["pipeline_failure"]["error"].lower()
    assert not policy_dispatch.called
    print(f"✅ PASS: Missing OPENAI_API_KEY → FAILED ({snap['payload']['pipeline_failure']['error'][:70]}…)")


def test_llm_failure_with_retries_left_does_not_fail_yet():
    Session = _make_session_factory()
    incident_id = _create_chaos_crash_incident(Session)
    llm = StubLLM(error=RuntimeError("Connection error"))
    try:
        _run_diagnosis(Session, incident_id, llm, _seeded_store(), retries=0)
        assert False, "expected the retry path to raise"
    except AssertionError:
        raise
    except Exception:
        pass
    assert _load(Session, incident_id)["status"] == IncidentStatus.CONTEXT_BUILT
    print("✅ PASS: First LLM failure retries instead of failing the incident")


def test_legacy_flag_still_chains_policy():
    """AI_INCIDENT_ANALYSIS=false keeps the old diagnosis → policy chain intact."""
    Session = _make_session_factory()
    db = Session()
    incident = Incident(alert_fingerprint="drill", alert_name="TargetAppDown", severity="critical",
                        status=IncidentStatus.CONTEXT_BUILT,
                        raw_payload_cache={"labels": {"alertname": "TargetAppDown", "namespace": "autofixops"}})
    db.add(incident)
    db.commit()
    incident_id = incident.id
    db.close()
    llm = StubLLM()
    guards = _run_diagnosis(Session, incident_id, llm, _seeded_store(), env={"AI_INCIDENT_ANALYSIS": "false"})
    snap = _load(Session, incident_id)
    assert snap["status"] == IncidentStatus.DIAGNOSED and snap["ai_report"] is None
    assert snap["diagnosed_by"] == "RULE_ENGINE" and not llm.prompts
    assert guards["evaluate_policy"].call_count == 1
    print("✅ PASS: AI_INCIDENT_ANALYSIS=false → legacy rule diagnosis still chains to policy")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("AI DRILL: Incident Analysis Stage")
    print("=" * 60 + "\n")
    test_context_reaches_ai_layer()
    test_relevant_runbooks_retrieved_and_passed_to_llm()
    test_structured_report_persisted_and_pipeline_stops()
    test_runbook_citations_labelled_official()
    test_historical_claims_rejected()
    test_historical_claim_in_pipeline_fails_incident()
    test_no_relevant_runbook_is_explicit()
    test_llm_failure_marks_failed_with_original_error()
    test_missing_api_key_marks_failed()
    test_llm_failure_with_retries_left_does_not_fail_yet()
    test_legacy_flag_still_chains_policy()
    print("\n🎯 ALL CHECKS PASSED\n")
