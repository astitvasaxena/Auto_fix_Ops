"""
Debug tracing for the AI diagnosis pipeline.
==============================================
When DEBUG_PROMPTS=true (env var), captures the full prompt, raw LLM
response, parsed output, RAG context, rule-engine verdict, and timing
for every LLM call.  Each incident gets a JSON file in ``debug_traces/``.

Three consumers:
  1. Structured logging  — always emits key fields to the ``autofixops`` logger
  2. File dump           — writes ``debug_traces/<incident_id>.json`` when enabled
  3. API endpoint        — ``GET /api/v1/incidents/{id}/debug`` reads these files
                           (see api/main.py)

This module does NOT alter diagnosis behaviour in any way.
"""

import json
import logging
import os
import time
import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("autofixops")


def _get_debug_dir() -> Path:
    return Path(os.getenv("DEBUG_TRACES_DIR", "debug_traces"))


def is_debug_enabled() -> bool:
    val = os.getenv("DEBUG_PROMPTS", "false").strip().lower()
    return val in ("true", "1", "yes")


# ────────────────────────────────────────────
# Trace data model (plain dicts, JSON-safe)
# ────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def new_trace(incident_id: str) -> Dict[str, Any]:
    """Creates a fresh trace envelope for an incident."""
    return {
        "incident_id": incident_id,
        "created_at": _now_iso(),
        "debug_enabled": is_debug_enabled(),
        "stages": {},
    }


def load_or_new_trace(incident_id: str) -> Dict[str, Any]:
    """Loads existing trace from disk if available, otherwise creates a new trace."""
    existing = load_trace(incident_id)
    if existing:
        return existing
    return new_trace(incident_id)


def record_pre_diagnosis_context(trace: Dict, raw_context: Dict) -> None:
    """Records the raw context built before diagnosis runs."""
    trace["raw_context"] = _safe_serialise(raw_context)
    trace["stages"].setdefault("build_context", {})
    trace["stages"]["build_context"] = {
        "status": "COMPLETED",
        "captured_at": _now_iso(),
        "context": _safe_serialise(raw_context),
    }


def save_pre_diagnosis_trace(incident_id: str, raw_context: Dict) -> Optional[str]:
    """Creates/updates a trace and persists raw context to disk before diagnosis."""
    trace = load_or_new_trace(incident_id)
    record_pre_diagnosis_context(trace, raw_context)
    return save_trace(trace)


def record_stage_start(trace: Dict, stage: str) -> None:
    """Marks the beginning of a named pipeline stage."""
    trace["stages"].setdefault(stage, {})
    trace["stages"][stage]["started_at"] = _now_iso()
    trace["stages"][stage]["_start_ns"] = time.perf_counter_ns()


def record_stage_end(trace: Dict, stage: str, **extra) -> None:
    """Marks the end of a stage and records elapsed time + optional data."""
    info = trace["stages"].setdefault(stage, {})
    start_ns = info.pop("_start_ns", None)
    info["finished_at"] = _now_iso()
    if start_ns is not None:
        info["elapsed_ms"] = round((time.perf_counter_ns() - start_ns) / 1e6, 2)
    info.update(extra)


def record_llm_call(
    trace: Dict,
    stage: str,
    *,
    rendered_prompt: str,
    raw_response: str,
    parsed_output: Any,
    model: str,
    provider: str,
    elapsed_ms: float,
    error: Optional[str] = None,
) -> None:
    """Records the full LLM round-trip: prompt, raw response, parsed result."""
    info = trace["stages"].setdefault(stage, {})
    info["llm"] = {
        "model": model,
        "provider": provider,
        "rendered_prompt": rendered_prompt,
        "rendered_prompt_length": len(rendered_prompt),
        "raw_response": raw_response,
        "raw_response_length": len(raw_response) if raw_response else 0,
        "parsed_output": _safe_serialise(parsed_output),
        "elapsed_ms": round(elapsed_ms, 2),
        "error": error,
        "recorded_at": _now_iso(),
    }

    # Always log — even when file dump is off
    logger.info(
        f"[DEBUG:LLM:{stage}] model={model} provider={provider} "
        f"prompt_len={len(rendered_prompt)} response_len={len(raw_response) if raw_response else 0} "
        f"elapsed_ms={round(elapsed_ms, 2)}"
    )
    if error:
        logger.warning(f"[DEBUG:LLM:{stage}] error={error}")


def record_rag(
    trace: Dict,
    stage: str,
    *,
    query: str,
    collection: str,
    hits: List[Dict],
    status: str,
    min_score: float,
    top_k: int,
) -> None:
    """Records a RAG retrieval step."""
    info = trace["stages"].setdefault(stage, {})
    info["rag"] = {
        "collection": collection,
        "query": query,
        "min_score": min_score,
        "top_k": top_k,
        "status": status,
        "hit_count": len(hits),
        "hits": [
            {
                "runbook_id": h.get("runbook_id") or h.get("summary", "")[:60],
                "title": h.get("title", ""),
                "similarity_score": h.get("similarity_score"),
            }
            for h in hits
        ],
        "recorded_at": _now_iso(),
    }

    logger.info(
        f"[DEBUG:RAG:{stage}] collection={collection} status={status} "
        f"hits={len(hits)} query_preview={query[:80]!r}"
    )


def record_rule_engine(trace: Dict, verdict: Dict) -> None:
    """Records the deterministic rule-engine output."""
    info = trace["stages"].setdefault("rule_engine", {})
    info["verdict"] = _safe_serialise(verdict)
    info["recorded_at"] = _now_iso()

    logger.info(
        f"[DEBUG:RULE] classification={verdict.get('root_cause_classification')} "
        f"confidence={verdict.get('confidence')} "
        f"action={verdict.get('action', {}).get('type')}"
    )


def record_context(trace: Dict, context: Dict) -> None:
    """Records the structured incident context passed to the LLM."""
    info = trace["stages"].setdefault("context", {})
    info["incident_context"] = _safe_serialise(context)
    info["recorded_at"] = _now_iso()


# ────────────────────────────────────────────
# Persistence
# ────────────────────────────────────────────

def save_trace(trace: Dict) -> Optional[str]:
    """Writes the trace to ``debug_traces/<incident_id>.json`` when DEBUG_PROMPTS=true.

    Returns the file path on success, None otherwise.
    """
    if not is_debug_enabled():
        return None

    trace["saved_at"] = _now_iso()
    dirpath = _get_debug_dir()
    dirpath.mkdir(parents=True, exist_ok=True)
    filepath = dirpath / f"{trace['incident_id']}.json"

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, default=str, ensure_ascii=False)
        logger.info(f"[DEBUG] Trace saved → {filepath}")
        return str(filepath)
    except Exception as e:
        logger.error(f"[DEBUG] Failed to save trace: {e}")
        return None


def load_trace(incident_id: str) -> Optional[Dict]:
    """Reads a previously saved debug trace. Returns None if missing."""
    filepath = _get_debug_dir() / f"{incident_id}.json"
    if not filepath.exists():
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[DEBUG] Failed to load trace {filepath}: {e}")
        return None


# ────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────

def _safe_serialise(obj: Any) -> Any:
    """Converts an object to a JSON-safe form without crashing."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe_serialise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialise(v) for v in obj]
    # Pydantic models, datetimes, etc.
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return str(obj)
