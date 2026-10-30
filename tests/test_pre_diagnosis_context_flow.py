"""
Test: Pre-Diagnosis Incident Context Capture & Retrieval Flow
============================================================
Verifies:
1. Context created inside build_incident_context() is captured & persisted
   BEFORE diagnose_incident() runs.
2. GET /api/v1/incidents/{id}/debug returns:
   - raw_context (captured before diagnosis)
   - ai_ready_context (null before diagnosis, populated after)
   - debug_available: True
   - teacher_summary comparing pre-diagnosis vs AI diagnosis
3. GET /api/v1/incidents/{id}/pre-diagnosis-context returns raw context.
4. Raw context is strictly preserved and distinct from the AI report.
"""
import os
import sys
import json
import time

# Ensure workspace is on sys.path
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

from dotenv import load_dotenv
load_dotenv()

import asyncio
import httpx
from api.main import app
from api.database import SessionLocal
from api.models import Incident, IncidentStatus
import workers.tasks as tasks
from engine import debug_trace as _dt


async def test_crash_loop_context_lifecycle():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    db = SessionLocal()

    print("\n" + "=" * 60)
    print("TEST: Pre-Diagnosis Context Capture & Retrieval Flow")
    print("=" * 60)

    # 1. Create a fresh Crash Loop incident (simulating chaos ingestion)
    incident = Incident(
        alert_fingerprint=f"test-drill-crashloop-{int(time.time() * 1000)}",
        alert_name="ChaosInjection:CrashLoop",
        severity="HIGH",
        status=IncidentStatus.INGESTED,
        raw_payload_cache={
            "chaos": True,
            "fault_type": "crash_loop",
            "target_url": "http://localhost:8001",
            "labels": {
                "alertname": "ChaosInjection:CrashLoop",
                "namespace": "autofixops",
                "pod": "autofixops-target-app",
                "severity": "high",
            },
            "annotations": {
                "summary": "Target application process keeps restarting in namespace autofixops",
            },
        },
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    incident_id = str(incident.id)
    print(f"\n[STEP 1] Created fresh Crash Loop incident: {incident_id}")

    # Verify initial debug endpoint response (INGESTED state)
    resp_ingested = await client.get(f"/api/v1/incidents/{incident_id}/debug")
    assert resp_ingested.status_code == 200
    data_ingested = resp_ingested.json()
    assert data_ingested["status"] == "INGESTED"
    print("  \u2714 Ingested state verified via API")

    # 2. Run TASK 1: build_incident_context
    print("\n[STEP 2] Running TASK 1 (build_incident_context)...")
    # Execute build_incident_context directly
    # Note: we bypass the Celery async chain to inspect between Task 1 and Task 2
    from unittest.mock import patch
    with patch.object(tasks.diagnose_incident, "apply_async") as mock_diagnose_chain:
        success = tasks.build_incident_context.run(incident_id)
        assert success is True, "build_incident_context should return True"
        assert mock_diagnose_chain.called, "Should attempt to enqueue diagnose_incident"

    # Refresh DB
    db.expire_all()
    incident_after_t1 = db.query(Incident).filter(Incident.id == incident_id).first()
    assert incident_after_t1.status == IncidentStatus.CONTEXT_BUILT
    print(f"  \u2714 Incident transitioned to CONTEXT_BUILT")

    # Verify raw_context in PostgreSQL
    raw_payload = incident_after_t1.raw_payload_cache or {}
    assert "raw_context" in raw_payload, "raw_context must be persisted in PostgreSQL raw_payload_cache"
    raw_ctx = raw_payload["raw_context"]
    assert raw_ctx["stage"] == "PRE_DIAGNOSIS"
    assert raw_ctx["chaos"]["fault_type"] == "crash_loop"
    assert raw_ctx["alert"]["name"] == "ChaosInjection:CrashLoop"
    assert raw_ctx["target"]["pod"] == "autofixops-target-app"
    print("  \u2714 raw_context persisted in PostgreSQL with correct fields")

    # 3. Test API endpoints BEFORE diagnosis has run
    print("\n[STEP 3] Inspecting context BEFORE AI diagnosis runs...")

    # Dedicated pre-diagnosis endpoint
    resp_pre = await client.get(f"/api/v1/incidents/{incident_id}/pre-diagnosis-context")
    assert resp_pre.status_code == 200
    pre_data = resp_pre.json()
    assert pre_data["context_available"] is True
    assert pre_data["status"] == "CONTEXT_BUILT"
    assert pre_data["raw_context"]["stage"] == "PRE_DIAGNOSIS"
    print("  \u2714 GET /api/v1/incidents/{id}/pre-diagnosis-context returns raw_context successfully")

    # Main debug endpoint
    resp_debug_pre = await client.get(f"/api/v1/incidents/{incident_id}/debug")
    assert resp_debug_pre.status_code == 200
    debug_pre = resp_debug_pre.json()
    assert debug_pre["debug_available"] is True
    assert debug_pre["raw_context"] is not None
    assert debug_pre["raw_context"]["chaos"]["fault_type"] == "crash_loop"
    assert debug_pre["raw_context"]["alert"]["name"] == "ChaosInjection:CrashLoop"
    # AI context should be None before diagnosis
    assert debug_pre["ai_ready_context"] is None
    assert "AI diagnosis has not run yet" in debug_pre["messages"]["ai_ready_context"]
    assert debug_pre["teacher_summary"]["phase_1_before_diagnosis"]["captured_before_ai"] is True
    assert debug_pre["teacher_summary"]["phase_2_ai_diagnosis"]["diagnosis_status"] == "PENDING"
    print("  \u2714 GET /api/v1/incidents/{id}/debug: raw_context present, AI context pending, teacher summary accurate")

    # 4. Run TASK 2: diagnose_incident (AI Diagnosis)
    print("\n[STEP 4] Running TASK 2 (diagnose_incident) with stub LLM & in-memory Qdrant...")
    from qdrant_client import QdrantClient
    from engine.memory import QdrantMemoryStore
    from engine.runbooks import load_runbooks
    from engine.ai_diagnostics import AIDiagnosisEngine
    from langchain_core.runnables import RunnableLambda

    store = QdrantMemoryStore(client=QdrantClient(location=":memory:"))
    store.upsert_runbooks(load_runbooks())

    def _mock_llm_response(prompt_val):
        return json.dumps({
            "incident_summary": "Crash loop detected on target app pod.",
            "incident_type": "CRASH_LOOP",
            "observed_evidence": ["Alert ChaosInjection:CrashLoop fired"],
            "retrieved_runbook_evidence": [{
                "runbook_id": "k8s-repeated-container-failure",
                "guidance_used": "Runbook recommends checking container logs and crash count.",
            }],
            "root_cause_analysis": "The container is crash-looping due to fault injection.",
            "reasoning": ["Process terminated unexpectedly."],
            "recommended_remediation": "Investigate pod logs and check crash hook.",
            "proposed_action": {
                "type": "RESTART_POD",
                "target": "autofixops-target-app",
                "rationale": "Restart pod to recover from crash loop",
            },
            "risk_impact": {
                "level": "MEDIUM",
                "description": "Pod downtime during restart",
            },
            "confidence": 0.95,
        })

    mock_llm = RunnableLambda(lambda p: _mock_llm_response(p.to_string()))

    with patch.object(tasks, "QdrantMemoryStore", lambda: store), \
         patch.object(tasks, "AIDiagnosisEngine", lambda: AIDiagnosisEngine(llm=mock_llm)):
        tasks.diagnose_incident.run(incident_id)

    # 5. Inspect API response AFTER diagnosis has completed
    print("\n[STEP 5] Inspecting context AFTER AI diagnosis completed...")
    resp_debug_post = await client.get(f"/api/v1/incidents/{incident_id}/debug")
    assert resp_debug_post.status_code == 200
    debug_post = resp_debug_post.json()

    assert debug_post["status"] == "DIAGNOSED"
    assert debug_post["debug_available"] is True
    assert debug_post["raw_context"] is not None
    assert debug_post["ai_ready_context"] is not None
    assert debug_post["db_summary"]["classification"] == "CRASH_LOOP"
    assert debug_post["db_summary"]["confidence"] == 0.95

    # Verification: raw_context != AI diagnosis report
    print("\n[STEP 6] Verifying raw context vs AI diagnosis report...")
    raw = debug_post["raw_context"]
    ai_report = debug_post["db_summary"]["ai_report"]

    print(f"  Raw context stage: {raw['stage']}")
    print(f"  Raw context fault type: {raw['chaos']['fault_type']}")
    print(f"  AI report incident type: {ai_report['incident_type']}")
    print(f"  AI report proposed action: {ai_report['proposed_action']['type']}")

    # Raw context contains observed facts & telemetry
    assert "telemetry" in raw
    assert "chaos" in raw
    assert raw["stage"] == "PRE_DIAGNOSIS"

    # AI report contains LLM inference & recommendations
    assert "root_cause_analysis" in ai_report
    assert "proposed_action" in ai_report
    assert "retrieved_runbook_evidence" in ai_report

    # Crucial distinction: raw context has no LLM recommendations
    assert "proposed_action" not in raw
    assert "root_cause_analysis" not in raw
    print("  \u2714 Raw context is strictly distinct from the AI report")

    # Teacher summary verification
    ts = debug_post["teacher_summary"]
    assert ts["phase_1_before_diagnosis"]["captured_before_ai"] is True
    assert ts["phase_2_ai_diagnosis"]["diagnosis_status"] == "COMPLETED"
    assert ts["phase_2_ai_diagnosis"]["has_ai_report"] is True
    print("  \u2714 Teacher summary shows both Phase 1 (pre-diagnosis) and Phase 2 (post-diagnosis)")

    print("\n" + "=" * 60)
    print("\u2705 ALL PRE-DIAGNOSIS CONTEXT FLOW CHECKS PASSED!")
    print("=" * 60 + "\n")

    await client.aclose()
    db.close()


if __name__ == "__main__":
    asyncio.run(test_crash_loop_context_lifecycle())

