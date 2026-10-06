"""
AI Drill: LLM Provider Selection
=================================
AIDiagnosisEngine picks its LLM from LLM_PROVIDER (ollama | openai) and the
structured-output configuration matches the AIIncidentReport schema.

No model is ever invoked: constructing ChatOllama / ChatOpenAI makes no
network call, and the Ollama client is replaced by a recorder when a request
payload is inspected. Ollama does NOT need to be running.
"""
import os
import sys
import json
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from engine.ai_diagnostics import (
    AIDiagnosisEngine, AIFinalVerdict, AIIncidentReport, AIReportValidationError,
    build_llm, validate_report,
)

OLLAMA_ENV = {
    "LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen2.5:3b",
    "OLLAMA_BASE_URL": "http://localhost:11434", "OLLAMA_NUM_CTX": "8192",
}
REPORT_SCHEMA = AIIncidentReport.model_json_schema()


def _env(**overrides):
    """Isolated LLM env: drops any LLM_*/OLLAMA_*/OPENAI_* from the real environment."""
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("LLM_", "OLLAMA_", "OPENAI_"))}
    base.update(overrides)
    return patch.dict(os.environ, base, clear=True)


def test_ollama_provider_selected():
    with _env(**OLLAMA_ENV):
        engine = AIDiagnosisEngine()
    assert isinstance(engine.llm, ChatOllama)
    assert engine.provider == "ollama" and engine.model_name == "qwen2.5:3b"
    print("✅ PASS: LLM_PROVIDER=ollama → ChatOllama (qwen2.5:3b)")


def test_ollama_is_default_provider():
    with _env():
        engine = AIDiagnosisEngine()
    assert isinstance(engine.llm, ChatOllama) and engine.model_name == "qwen2.5:3b"
    print("✅ PASS: Unset LLM_PROVIDER defaults to local Ollama (no paid calls by accident)")


def test_ollama_model_configuration():
    with _env(LLM_PROVIDER="ollama", OLLAMA_MODEL="llama3.2:3b",
              OLLAMA_BASE_URL="http://10.0.0.5:11434", OLLAMA_NUM_CTX="16384"):
        llm, provider, model = build_llm()
    assert (provider, model) == ("ollama", "llama3.2:3b")
    assert llm.model == "llama3.2:3b"
    assert llm.base_url == "http://10.0.0.5:11434"
    assert llm.num_ctx == 16384
    assert llm.temperature == 0.0
    assert llm.num_predict and llm.num_predict > 0
    print("✅ PASS: OLLAMA_MODEL / OLLAMA_BASE_URL / OLLAMA_NUM_CTX are applied; temperature 0")


def test_ollama_default_configuration():
    with _env(LLM_PROVIDER="ollama"):
        llm, _, model = build_llm()
    assert model == "qwen2.5:3b" and llm.base_url == "http://localhost:11434" and llm.num_ctx == 8192
    print("✅ PASS: Ollama defaults are qwen2.5:3b @ localhost:11434 with num_ctx=8192")


def test_ollama_structured_output_is_report_schema():
    with _env(**OLLAMA_ENV):
        engine = AIDiagnosisEngine()
    assert engine.llm.format == REPORT_SCHEMA

    # Inspect the exact request payload without contacting a server.
    params = engine.llm._chat_params([HumanMessage(content="x")])
    assert params["format"] == REPORT_SCHEMA
    assert params["model"] == "qwen2.5:3b"
    assert params["options"]["num_ctx"] == 8192
    assert params["options"]["temperature"] == 0.0
    print("✅ PASS: Ollama request carries format=AIIncidentReport schema and num_ctx=8192")


def test_legacy_verdict_chain_uses_its_own_schema():
    with _env(**OLLAMA_ENV):
        engine = AIDiagnosisEngine()
    bound = engine.verdict_llm.kwargs.get("format")
    assert bound == AIFinalVerdict.model_json_schema()
    print("✅ PASS: Legacy analyze() binds the AIFinalVerdict schema, not the report schema")


def test_openai_provider_selected():
    with _env(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test-not-real", OPENAI_MODEL_NAME="gpt-4o-mini"):
        engine = AIDiagnosisEngine()
    assert isinstance(engine.llm, ChatOpenAI)
    assert engine.provider == "openai" and engine.model_name == "gpt-4o-mini"
    assert engine.llm.temperature == 0.0
    assert engine.verdict_llm is engine.llm
    print("✅ PASS: LLM_PROVIDER=openai → existing ChatOpenAI (gpt-4o-mini) fallback")


def test_openai_without_key_still_fails_loudly():
    with _env(LLM_PROVIDER="openai"):
        try:
            AIDiagnosisEngine()
            assert False, "ChatOpenAI without a key should raise"
        except AssertionError:
            raise
        except Exception as e:
            assert "api_key" in str(e).lower() or "credentials" in str(e).lower()
    print("✅ PASS: OpenAI provider without a key raises (pipeline turns this into FAILED)")


def test_unknown_provider_rejected():
    with _env(LLM_PROVIDER="claude-local"):
        try:
            AIDiagnosisEngine()
            assert False, "unknown provider accepted"
        except ValueError as e:
            assert "claude-local" in str(e)
    print("✅ PASS: Unknown LLM_PROVIDER raises a clear ValueError")


def test_injected_llm_still_supported():
    stub = RunnableLambda(lambda pv: "{}")
    engine = AIDiagnosisEngine(llm=stub)
    assert engine.llm is stub and engine.provider == "injected" and engine.verdict_llm is stub
    print("✅ PASS: Injected stub LLMs bypass the factory (existing tests keep working)")


def test_report_carries_model_and_provider_labels():
    """End-to-end through analyze_incident with a recorder in place of the Ollama HTTP client."""
    with _env(**OLLAMA_ENV):
        engine = AIDiagnosisEngine()

    report_json = json.dumps({
        "incident_summary": "s", "incident_type": "HIGH_MEMORY", "observed_evidence": ["e"],
        "retrieved_runbook_evidence": [], "root_cause_analysis": "r", "reasoning": ["x"],
        "recommended_remediation": "m",
        "proposed_action": {"type": "ESCALATE", "target": "t", "rationale": "why"},
        "confidence": 0.5, "risk_impact": {"level": "LOW", "description": "d"},
    })
    seen = {}

    def fake_chat(**kwargs):
        seen.update(kwargs)
        yield {"model": "qwen2.5:3b", "created_at": "2026-10-06T00:00:00Z", "done": True,
               "done_reason": "stop", "message": {"role": "assistant", "content": report_json}}

    with patch.object(engine.llm._client, "chat", side_effect=fake_chat):
        report = engine.analyze_incident({"alert": {"name": "HighMemoryUsage"}}, {}, [], {"min_score": 0.35})

    assert seen["format"] == REPORT_SCHEMA and seen["options"]["num_ctx"] == 8192
    assert report["model"] == "qwen2.5:3b" and report["llm_provider"] == "ollama"
    assert report["incident_type"] == "HIGH_MEMORY" and report["requires_human_approval"] is True
    print("✅ PASS: Report via (mocked) Ollama is labelled model=qwen2.5:3b, llm_provider=ollama")


def test_validation_is_provider_independent():
    """The same safety checks reject bad output regardless of which provider produced it."""
    bad = AIIncidentReport.model_validate({
        "incident_summary": "This matches a previous incident.", "incident_type": "CRASH_LOOP",
        "observed_evidence": [], "retrieved_runbook_evidence": [{"runbook_id": "runbook-x", "guidance_used": "g"}],
        "root_cause_analysis": "r", "reasoning": [], "recommended_remediation": "m",
        "proposed_action": {"type": "ESCALATE", "target": "t", "rationale": "y"},
        "confidence": 0.4, "risk_impact": {"level": "LOW", "description": "d"},
    })
    for provider_env in (OLLAMA_ENV, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test-not-real"}):
        with _env(**provider_env):
            AIDiagnosisEngine()  # provider builds; validation below is shared code
        try:
            validate_report(bad, hits=[])
            assert False, "invalid report accepted"
        except AIReportValidationError:
            pass
    try:
        AIIncidentReport.model_validate({"confidence": 2.0})
        assert False, "schema accepted invalid report"
    except Exception:
        pass
    print("✅ PASS: Report validation and schema checks are identical for both providers")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("AI DRILL: LLM Provider Selection")
    print("=" * 60 + "\n")
    test_ollama_provider_selected()
    test_ollama_is_default_provider()
    test_ollama_model_configuration()
    test_ollama_default_configuration()
    test_ollama_structured_output_is_report_schema()
    test_legacy_verdict_chain_uses_its_own_schema()
    test_openai_provider_selected()
    test_openai_without_key_still_fails_loudly()
    test_unknown_provider_rejected()
    test_injected_llm_still_supported()
    test_report_carries_model_and_provider_labels()
    test_validation_is_provider_independent()
    print("\n🎯 ALL CHECKS PASSED\n")
