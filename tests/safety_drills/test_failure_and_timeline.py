"""
Safety Drill: Exhausted Retries + Status Timeline Persistence
==============================================================
1. When a pipeline task exhausts its Celery retries, the incident must end
   in FAILED (with the original error recorded), not stay stuck mid-pipeline.
2. Every Incident.transition_to() must be persisted in status_timeline,
   not just the first one.

Runs against an in-memory SQLite database with stubbed engines — no
Postgres, Redis, Prometheus, OpenAI or GitHub access needed.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from contextlib import ExitStack
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.database import Base
from api.models import Incident, IncidentStatus
import workers.tasks as tasks
import engine.circuit_breaker as circuit_breaker


def _make_session_factory():
    db_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(db_engine)
    return sessionmaker(bind=db_engine, autocommit=False, autoflush=False)


def _create_incident(Session, status, alert_name="TargetAppDown"):
    db = Session()
    incident = Incident(
        alert_fingerprint="drill",
        alert_name=alert_name,
        severity="critical",
        status=status,
        raw_payload_cache={
            "labels": {"alertname": alert_name, "namespace": "autofixops", "pod": "autofixops-target-app"}
        },
    )
    db.add(incident)
    db.commit()
    # Kept as uuid.UUID: SQLite's UUID type can't bind the str ids Postgres accepts.
    incident_id = incident.id
    db.close()
    return incident_id


def _load(Session, incident_id):
    db = Session()
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    status, payload, timeline = incident.status, incident.raw_payload_cache, incident.status_timeline
    db.close()
    return status, payload, timeline


def _run_task(task, retries, *args):
    """Runs a bound task body with a simulated retry count."""
    task.push_request(retries=retries)
    try:
        return task.run(*args)
    finally:
        task.pop_request()


class _StubMemoryStore:
    def retrieve_similar(self, *a, **kw):
        return []

    def retrieve_failures(self, *a, **kw):
        return []


class _MissingKeyAIEngine:
    def __init__(self):
        raise RuntimeError("The api_key client option must be set")


class _FailingRemediationEngine:
    def __init__(self, project_config=None):
        pass

    def execute(self, *a, **kw):
        raise RuntimeError("GitHub API returned 502")


class _AllowAllRegistry:
    def should_allow(self, *a, **kw):
        return True


def _pipeline_patches(Session):
    stack = ExitStack()
    stack.enter_context(patch.object(tasks, "SessionLocal", Session))
    stack.enter_context(patch.object(tasks, "_check_kill_switch", lambda _id: False))
    stack.enter_context(patch.object(tasks, "emit_sync", lambda *a, **kw: None))
    return stack


# ─── Timeline persistence ───

def test_timeline_persists_across_sessions():
    """INGESTED → CONTEXT_BUILT → DIAGNOSED, each in its own session (as the Celery tasks do)."""
    Session = _make_session_factory()
    incident_id = _create_incident(Session, IncidentStatus.INGESTED)

    for new_status in (IncidentStatus.CONTEXT_BUILT, IncidentStatus.DIAGNOSED):
        db = Session()
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        incident.transition_to(new_status)
        db.commit()
        db.close()

    status, _, timeline = _load(Session, incident_id)
    assert status == IncidentStatus.DIAGNOSED
    assert [t["status"] for t in timeline] == ["CONTEXT_BUILT", "DIAGNOSED"], timeline
    assert all("timestamp" in t for t in timeline)
    print("✅ PASS: Timeline persisted both transitions across sessions")


def test_timeline_persists_within_one_session():
    """Multiple commits in one session (as VerificationEngine does for VERIFIED → RESOLVED)."""
    Session = _make_session_factory()
    incident_id = _create_incident(Session, IncidentStatus.INGESTED)

    db = Session()
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    incident.transition_to(IncidentStatus.CONTEXT_BUILT)
    db.commit()
    incident.transition_to(IncidentStatus.DIAGNOSED)
    db.commit()
    db.close()

    _, _, timeline = _load(Session, incident_id)
    assert [t["status"] for t in timeline] == ["CONTEXT_BUILT", "DIAGNOSED"], timeline
    print("✅ PASS: Timeline persisted both transitions within one session")


# ─── Exhausted retries → FAILED ───

def test_diagnosis_exhausted_retries_marks_failed():
    """AI path crashes (e.g. missing OpenAI key) on the final attempt → FAILED with the real error."""
    Session = _make_session_factory()
    incident_id = _create_incident(Session, IncidentStatus.CONTEXT_BUILT, "ChaosInjection:CrashLoop")

    with _pipeline_patches(Session), \
         patch.object(tasks, "QdrantMemoryStore", _StubMemoryStore), \
         patch.object(tasks, "AIDiagnosisEngine", _MissingKeyAIEngine):
        task = tasks.diagnose_incident
        result = _run_task(task, task.max_retries, incident_id)

    status, payload, timeline = _load(Session, incident_id)
    assert result is False
    assert status == IncidentStatus.FAILED, status
    failure = payload["pipeline_failure"]
    assert failure["stage"] == "diagnose_incident"
    assert "api_key client option must be set" in failure["error"]
    assert timeline[-1]["status"] == "FAILED"
    print("✅ PASS: diagnose_incident exhausted retries → FAILED with original error")


def test_context_exhausted_retries_marks_failed():
    Session = _make_session_factory()
    incident_id = _create_incident(Session, IncidentStatus.INGESTED)

    def _boom(*a, **kw):
        raise RuntimeError("unexpected context failure")

    with _pipeline_patches(Session), patch.object(tasks, "fetch_prometheus_metric", _boom):
        task = tasks.build_incident_context
        result = _run_task(task, task.max_retries, incident_id)

    status, payload, _ = _load(Session, incident_id)
    assert result is False
    assert status == IncidentStatus.FAILED, status
    assert payload["pipeline_failure"]["stage"] == "build_incident_context"
    assert "unexpected context failure" in payload["pipeline_failure"]["error"]
    print("✅ PASS: build_incident_context exhausted retries → FAILED")


def test_remediation_exhausted_retries_marks_failed():
    """Failure after REMEDIATING was committed must not leave the incident stuck in REMEDIATING."""
    Session = _make_session_factory()
    incident_id = _create_incident(Session, IncidentStatus.POLICY_APPROVED)
    verdict = {"action": {"type": "RESTART_POD", "target": "autofixops-target-app"}}

    with _pipeline_patches(Session), \
         patch.object(tasks, "RemediationEngine", _FailingRemediationEngine), \
         patch.object(circuit_breaker, "get_circuit_breaker_registry", lambda: _AllowAllRegistry()):
        task = tasks.execute_remediation
        result = _run_task(task, task.max_retries, incident_id, verdict)

    status, payload, timeline = _load(Session, incident_id)
    assert result is False
    assert status == IncidentStatus.FAILED, status
    assert "GitHub API returned 502" in payload["pipeline_failure"]["error"]
    assert [t["status"] for t in timeline] == ["REMEDIATING", "FAILED"], timeline
    print("✅ PASS: execute_remediation exhausted retries → FAILED (not stuck in REMEDIATING)")


def test_retries_remaining_does_not_mark_failed():
    """With retries left, the task must still retry rather than fail the incident early."""
    Session = _make_session_factory()
    incident_id = _create_incident(Session, IncidentStatus.CONTEXT_BUILT, "ChaosInjection:CrashLoop")

    with _pipeline_patches(Session), \
         patch.object(tasks, "QdrantMemoryStore", _StubMemoryStore), \
         patch.object(tasks, "AIDiagnosisEngine", _MissingKeyAIEngine):
        try:
            _run_task(tasks.diagnose_incident, 0, incident_id)
            assert False, "Expected the retry path to raise"
        except AssertionError:
            raise
        except Exception:
            pass  # Retry (or, outside a worker, the original exception) propagates

    status, payload, _ = _load(Session, incident_id)
    assert status == IncidentStatus.CONTEXT_BUILT, status
    assert "pipeline_failure" not in (payload or {})
    print("✅ PASS: First failure retries instead of marking FAILED")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("SAFETY DRILL: Exhausted Retries + Timeline Persistence")
    print("=" * 60 + "\n")
    test_timeline_persists_across_sessions()
    test_timeline_persists_within_one_session()
    test_diagnosis_exhausted_retries_marks_failed()
    test_context_exhausted_retries_marks_failed()
    test_remediation_exhausted_retries_marks_failed()
    test_retries_remaining_does_not_mark_failed()
    print("\n🎯 ALL CHECKS PASSED\n")
