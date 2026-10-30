"""
AI patch proposer (Stage 2): propose and validate, never apply.
================================================================
Given a DIAGNOSED incident's stored ai_report, proposes a bounded memory or
CPU limit increase for the single allowlisted manifest and validates it in a
temporary sandbox. The result is stored in patch_proposals. Nothing here
applies, commits, pushes, deploys or opens a PR; human approval is Stage 3.

Flow
  1. Deterministic gates (no LLM): kill switch, diagnosis validity, supported
     action, confidence, telemetry utilisation, official runbook evidence,
     target path/file safety, manifest shape, identity, repo, bounds.
     Any failure -> NOT_GENERATED/<code>.
  2. Code computes a small menu of allowed values within PatchGenerator bounds.
  3. The LLM (local Ollama by default) may only choose one menu value or
     decline, citing fact ids. Its output is schema-constrained and re-checked.
  4. Code splices that one scalar (comments/quoting preserved) and builds a
     minimal unified diff, which is treated as untrusted and validated
     (V6-V13): bounds, render, diff policy, sandbox apply, YAML scope,
     PatchGenerator oracle + rollback, EOL variant, real file untouched.

  5. Before a VALIDATED result is stored, finalize() re-reads the incident and
     the kill switch under a row lock; any change discards the diff.

Confidence routing (engine.routing_policy): a report at or above 0.80 takes the
AUTOMATIC_AI route. Below 0.80 the confidence gate passes only if the database
holds an APPROVED routing row for this incident and this exact report (sha256).
run_for_incident reads it from the database (propose() takes it as an argument
for tests), and finalize() re-derives the route from the stored report and
re-reads the approval in the writing transaction, so a VALIDATED row below 0.80
always has a matching approval. Approval replaces the confidence gate only.

Statuses: VALIDATED | NOT_GENERATED | REJECTED | FAILED (RUNNING while in
progress). A diff is stored only for VALIDATED; finalize() enforces this.

Settings (environment or Settings(...); every value is validated, and the
defaults are also the hard limits, so values can only be tightened):
  PATCH_LLM_TIMEOUT_SECONDS (300, max)   PATCH_LLM_MAX_TOKENS (384, max)
  PATCH_PROPOSAL_VALIDATION_TIMEOUT_SECONDS (15, max)
  PATCH_PROPOSAL_MIN_CONFIDENCE (0.80, fixed: any other value is CONFIG_INVALID, as is a
  project confidence_threshold other than 0.80)   PATCH_PROPOSAL_MIN_UTILISATION (0.80, min)
  PATCH_PROPOSAL_MAX_FACTOR (2.0, max)
A RUNNING row is considered active for STALE_AFTER_S (fixed, derived from the
hard limits), whatever settings the reader uses.

Reason codes by category:
  gates/config  SYSTEM_DISABLED INCIDENT_NOT_DIAGNOSED NO_AI_REPORT REPORT_INVALID
                UNSUPPORTED_ACTION INCONSISTENT_DIAGNOSIS LOW_CONFIDENCE INSUFFICIENT_EVIDENCE
                NO_SUPPORTING_RUNBOOK CONFIG_INVALID TARGET_MISMATCH RESTRICTED_NAMESPACE
                REPO_MISMATCH AT_BOUND INCIDENT_CHANGED APPROVAL_INVALID, plus path/manifest codes from
                patch_safety and patch_diff (e.g. PATH_REJECTED, UNSUPPORTED_MANIFEST)
  model         MODEL_DECLINED NO_EVIDENCE_CITED LLM_MALFORMED_OUTPUT LLM_INVALID_CHOICE
                UNGROUNDED_CLAIM LLM_UNAVAILABLE LLM_TIMEOUT LLM_ERROR
  validation    BOUNDS_VIOLATION RENDER_FAILED DIFF_POLICY_VIOLATION APPLY_FAILED YAML_INVALID
                OUT_OF_SCOPE_CHANGE LIMIT_BELOW_REQUEST CROSS_CHECK_MISMATCH EOL_MISMATCH
                BASE_CHANGED DEADLINE_EXCEEDED
  database      CONFIG_UNAVAILABLE (config read failed) PERSISTENCE_FAILED (write/read-back failed)
  other         INTERRUPTED INTERNAL_ERROR INTERNAL_INVARIANT; ABANDONED and INTEGRITY_MISMATCH
                are computed by get_latest_proposal() and never stored
"""

import copy
import datetime
import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import yaml
from pydantic import ConfigDict, Field, ValidationError, create_model
from sqlalchemy import null as sa_null

from engine.ai_diagnostics import AIIncidentReport, _HISTORICAL_CLAIM_PATTERNS, build_llm
from engine.patch_diff import (
    PatchDiffError, assert_plain_document, changed_paths, check_diff_policy, locate_container_scalar,
    make_unified_diff, parse_unified_diff_strict, render_scalar, sandbox_apply, splice_scalar,
)
from engine.patch_generator import HARD_CAPS, PatchGenerator, format_cpu, format_memory, parse_cpu, parse_memory
from engine.patch_safety import (
    TARGET_MANIFEST, PatchSafetyError, allowlisted_field, git_blob_sha, read_allowlisted_file,
    rehash, validate_repo_relative_path,
)
from engine.policy import CONFIDENCE_FLOOR, RESTRICTED_NAMESPACES
from engine.routing_policy import (
    APPROVED, AUTOMATIC_AI, HUMAN_APPROVAL_REQUIRED, HUMAN_APPROVED, threshold_conflict,
)
from engine.runbooks import RUNBOOK_VERSION, load_runbooks

PROPOSER_VERSION = 1
PROMPT_VERSION = "patch-decision/2"

RUNNING, VALIDATED, NOT_GENERATED, REJECTED, FAILED = (
    "RUNNING", "VALIDATED", "NOT_GENERATED", "REJECTED", "FAILED")

EXIT_CODES = {VALIDATED: 0, NOT_GENERATED: 2, REJECTED: 3, FAILED: 4}
EXIT_USAGE, EXIT_NOT_PERSISTED, EXIT_BUSY, EXIT_ALREADY_GENERATED = 1, 5, 6, 7

_MIB = 1024 ** 2

# Supported remediations. Everything else (RESTART_POD, ROLLBACK_DEPLOYMENT,
# ESCALATE) is refused before any LLM call.
ACTIONS = {
    "INCREASE_MEMORY_LIMIT": {
        "resource": "memory", "incident_type": "HIGH_MEMORY", "peak_key": "memory_peak",
        "conflicting_rule": "CPU_RESOURCE_STARVATION", "runbook": "k8s-oomkilled-memory-pressure",
        "parse": parse_memory, "format": format_memory,
    },
    "INCREASE_CPU_LIMIT": {
        "resource": "cpu", "incident_type": "HIGH_CPU", "peak_key": "cpu_peak",
        "conflicting_rule": "MEMORY_LEAK_OOM_RISK", "runbook": "k8s-cpu-limits-throttling",
        "parse": parse_cpu, "format": format_cpu,
    },
}
CANDIDATE_MULTIPLIERS = (1.25, 1.5, 2.0)
DECLINE_REASONS = ("NONE", "INSUFFICIENT_EVIDENCE", "LEAK_SUSPECTED", "NOT_CAPACITY_PROBLEM", "TARGET_UNCLEAR")
TELEMETRY_FACT_IDS = ("F3", "F4")
MANDATORY_CHECKS = (
    "confidence_route", "path_syntax", "path_allowlist", "path_containment", "manifest_shape", "identity_and_config",
    "llm_decision", "bounds", "render_textual", "diff_policy", "sandbox_apply",
    "post_apply_yaml", "patchgenerator_oracle", "eol_variant", "real_tree_untouched",
)
POLICY_MIN_UTILISATION = 0.80
POLICY_MAX_FACTOR = 2.0
MAX_PLAUSIBLE_UTILISATION = 1.5   # a peak far above the limit means the manifest is not what ran
EXIT_INTERRUPTED = 130
_LABEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,253}")
_REPO_PART_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_ACTION_TYPE_RE = re.compile(r"[A-Z_]{1,32}")
_NUMBER_RE = re.compile(r"(?<!\d)(?<!\d\.)\d+(?:\.\d+)?")
# Control, invisible-formatting (e.g. bidi overrides), separator, private-use and surrogate characters.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs"})
_PROPOSER_HISTORY_RE = re.compile(
    r"\b(previously|historically|last time|in the past|as before|once before"
    r"|(fixed|resolved|solved|worked|helped)\s+((this|it|that)\s+)?(before|earlier))\b", re.IGNORECASE)


# ─── Settings and outcomes ───

# Approved hard limits. Settings may only be stricter than these.
MAX_LLM_TIMEOUT_S = 300.0
MAX_LLM_TOKENS = 384
MAX_VALIDATION_TIMEOUT_S = 15.0
MAX_GUARD_MARGIN_S = 30.0
# A RUNNING row younger than this is treated as active by every caller. Derived
# from the hard limits only, so no caller's settings can shorten or lengthen it,
# and no run can outlive it (LLM guard + validation deadline + slack).
STALE_AFTER_S = MAX_LLM_TIMEOUT_S + MAX_GUARD_MARGIN_S + MAX_VALIDATION_TIMEOUT_S + 60.0


def _is_real(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _at_least(value, floor):
    """NaN-safe max(value, floor) for an already-validated threshold."""
    return value if _is_real(value) and value >= floor else floor


@dataclass(frozen=True)
class Settings:
    llm_timeout_s: float = MAX_LLM_TIMEOUT_S
    llm_max_tokens: int = MAX_LLM_TOKENS
    min_confidence: float = CONFIDENCE_FLOOR
    min_utilisation: float = POLICY_MIN_UTILISATION
    max_factor: float = POLICY_MAX_FACTOR
    validation_timeout_s: float = MAX_VALIDATION_TIMEOUT_S
    guard_margin_s: float = MAX_GUARD_MARGIN_S   # wall-clock guard = HTTP timeout + margin

    def __post_init__(self):
        for name in ("llm_timeout_s", "min_confidence", "min_utilisation", "max_factor",
                     "validation_timeout_s", "guard_margin_s"):
            value = getattr(self, name)
            if not _is_real(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number, got {value!r}")
        if not isinstance(self.llm_max_tokens, int) or isinstance(self.llm_max_tokens, bool):
            raise ValueError(f"llm_max_tokens must be an integer, got {self.llm_max_tokens!r}")
        limits = (
            ("llm_timeout_s", self.llm_timeout_s <= MAX_LLM_TIMEOUT_S, f"at most {MAX_LLM_TIMEOUT_S:g}"),
            ("llm_max_tokens", 1 <= self.llm_max_tokens <= MAX_LLM_TOKENS, f"between 1 and {MAX_LLM_TOKENS}"),
            ("validation_timeout_s", self.validation_timeout_s <= MAX_VALIDATION_TIMEOUT_S,
             f"at most {MAX_VALIDATION_TIMEOUT_S:g}"),
            ("guard_margin_s", self.guard_margin_s <= MAX_GUARD_MARGIN_S, f"at most {MAX_GUARD_MARGIN_S:g}"),
            # One threshold for routing and generation: a stricter value would route an incident
            # automatically and then refuse to generate it, so it is rejected, not tightened.
            ("min_confidence", threshold_conflict(self.min_confidence, "min_confidence") is None,
             f"exactly the routing threshold {CONFIDENCE_FLOOR:.2f}"),
            ("min_utilisation", POLICY_MIN_UTILISATION <= self.min_utilisation <= MAX_PLAUSIBLE_UTILISATION,
             f"between {POLICY_MIN_UTILISATION} and {MAX_PLAUSIBLE_UTILISATION}"),
            ("max_factor", 1.0 < self.max_factor <= POLICY_MAX_FACTOR, f"above 1.0 and at most {POLICY_MAX_FACTOR}"),
        )
        for name, ok, rule in limits:
            if not ok:
                raise ValueError(f"{name} must be {rule}, got {getattr(self, name)!r}")

    @property
    def guard_s(self) -> float:
        return self.llm_timeout_s + self.guard_margin_s


_ENV_SETTINGS = (
    ("PATCH_LLM_TIMEOUT_SECONDS", "llm_timeout_s", float),
    ("PATCH_LLM_MAX_TOKENS", "llm_max_tokens", int),
    ("PATCH_PROPOSAL_MIN_CONFIDENCE", "min_confidence", float),
    ("PATCH_PROPOSAL_MIN_UTILISATION", "min_utilisation", float),
    ("PATCH_PROPOSAL_MAX_FACTOR", "max_factor", float),
    ("PATCH_PROPOSAL_VALIDATION_TIMEOUT_SECONDS", "validation_timeout_s", float),
)


def settings_from_env(env=None) -> Settings:
    """Settings from PATCH_* variables; raises ValueError for anything outside the approved limits."""
    env = os.environ if env is None else env
    values = {}
    for name, field_name, cast in _ENV_SETTINGS:
        raw = env.get(name)
        if raw in (None, ""):
            continue
        try:
            values[field_name] = cast(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{name} is not a valid {cast.__name__}")
    try:
        return Settings(**values)
    except ValueError as exc:
        names = {f: n for n, f, _ in _ENV_SETTINGS}
        message = str(exc)
        field_name = message.split(" ", 1)[0]
        raise ValueError(message.replace(field_name, names.get(field_name, field_name), 1))


class ProposalStop(Exception):
    """Ends a proposal run with a terminal status and reason code."""

    def __init__(self, status: str, code: str, detail: str = ""):
        super().__init__(f"{status}/{code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class _GuardTimeout(Exception):
    pass


def _sanitize(text, limit: int) -> str:
    cleaned = "".join(ch if ch in "\n\t" or unicodedata.category(ch) not in _UNSAFE_CATEGORIES else "?"
                      for ch in str(text))
    return cleaned[:limit]


def _one_line(text, limit: int) -> str:
    return " ".join(_sanitize(text, limit * 2).split())[:limit]


def _plain_label(value) -> Optional[str]:
    return value if isinstance(value, str) and _LABEL_RE.fullmatch(value) else None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _error_summary(exc: BaseException) -> str:
    """Exception class names only: database errors carry SQL text and bound parameters."""
    name = type(exc).__name__
    orig = getattr(exc, "orig", None)
    return f"{name}/{type(orig).__name__}" if orig is not None else name


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


class _Deadline:
    def __init__(self, seconds: float, clock=time.monotonic):
        self._clock, self._end = clock, clock() + seconds

    def check(self, where: str):
        if self._clock() > self._end:
            raise ProposalStop(FAILED, "DEADLINE_EXCEEDED", f"validation deadline exceeded before {where}")


class _Steps:
    def __init__(self, items: Optional[List[dict]] = None):
        self.items: List[dict] = [] if items is None else items

    def add(self, check: str, status: str, code: Optional[str] = None, detail: str = "", t0: float = None):
        elapsed = int((time.monotonic() - t0) * 1000) if t0 is not None else 0
        self.items.append({"check": check, "status": status, "code": code,
                           "detail": _one_line(detail, 300), "elapsed_ms": elapsed})


# ─── Snapshot and gates (pure) ───

def snapshot_incident(incident) -> dict:
    payload = incident.raw_payload_cache or {}
    status = incident.status.value if hasattr(incident.status, "value") else str(incident.status)
    return {
        "id": str(incident.id), "status": status, "diagnosed_by": incident.diagnosed_by,
        "ai_report": copy.deepcopy(incident.ai_report),
        "raw_context": copy.deepcopy(payload.get("raw_context")) if isinstance(payload, dict) else None,
        "project_id": str(incident.project_id) if incident.project_id else None,
    }


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _field(container: dict, key: str, kind, where: str):
    """container[key] if it has the expected JSON type (missing -> empty), else REPORT_INVALID."""
    value = container.get(key)
    if value is None:
        return kind()
    if not isinstance(value, kind):
        raise ProposalStop(NOT_GENERATED, "REPORT_INVALID",
                           f"{where}.{key} must be {'an object' if kind is dict else 'a list'}")
    return value


def _report_gates(snap: dict, settings: Settings, system_mode: str, approval=None) -> dict:
    if system_mode == "DISABLED":
        raise ProposalStop(NOT_GENERATED, "SYSTEM_DISABLED", "kill switch is DISABLED")
    if snap["status"] != "DIAGNOSED":
        raise ProposalStop(NOT_GENERATED, "INCIDENT_NOT_DIAGNOSED", f"status is {snap['status']}")
    report = snap["ai_report"]
    if not isinstance(report, dict) or snap["diagnosed_by"] != "AI_ENGINE":
        raise ProposalStop(NOT_GENERATED, "NO_AI_REPORT", "no stored AI incident report")
    if report.get("report_version") != 1 or report.get("role") != "ANALYST_ONLY":
        raise ProposalStop(NOT_GENERATED, "REPORT_INVALID", "unexpected report version or role")
    try:
        # strict: no coercion (a confidence of True or "0.95" is invalid, not 1.0 / 0.95).
        parsed = AIIncidentReport.model_validate({k: report.get(k) for k in AIIncidentReport.model_fields},
                                                 strict=True)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err.get("loc", ())[:1]) for err in exc.errors()})
        raise ProposalStop(NOT_GENERATED, "REPORT_INVALID",
                           f"{len(exc.errors())} schema error(s) in {', '.join(fields)[:200]}")
    # Nested objects the gates read below; wrong JSON types are report errors, not crashes.
    rule_engine = _field(report, "rule_engine", dict, "report")
    rule_action = _field(rule_engine, "action", dict, "rule_engine")
    context = _field(report, "incident_context", dict, "report")
    telemetry = _field(context, "telemetry", dict, "incident_context")
    summary = _field(telemetry, "summary", dict, "telemetry")
    alert = _field(context, "alert", dict, "incident_context")
    labels = _field(alert, "labels", dict, "alert")
    rag = _field(report, "rag", dict, "report")
    retrieved_hits = _field(rag, "retrieved", list, "rag")
    raw_context = snap.get("raw_context")
    if raw_context is not None and not isinstance(raw_context, dict):
        raise ProposalStop(NOT_GENERATED, "REPORT_INVALID", "incident raw_context must be an object")
    raw_tel = _field(raw_context or {}, "telemetry", dict, "raw_context")

    action = parsed.proposed_action.type
    spec = ACTIONS.get(action)
    if spec is None:
        raise ProposalStop(NOT_GENERATED, "UNSUPPORTED_ACTION", f"{action} is not supported in Stage 2")
    rule_class = rule_engine.get("root_cause_classification")
    if parsed.incident_type != spec["incident_type"] or rule_class == spec["conflicting_rule"]:
        raise ProposalStop(NOT_GENERATED, "INCONSISTENT_DIAGNOSIS",
                           f"{action} conflicts with incident_type {parsed.incident_type} / rule {_plain_label(rule_class)}")
    # The one routing threshold, never the (validated, but possibly forced) settings value.
    min_confidence = CONFIDENCE_FLOOR
    if not _is_real(parsed.confidence):
        raise ProposalStop(NOT_GENERATED, "REPORT_INVALID", "confidence is not a finite number")
    if parsed.confidence >= min_confidence:
        routing = {"mode": AUTOMATIC_AI, "approval_required": False, "decision_id": None}
    else:
        # Below the floor, only a recorded human approval of this exact report allows generation.
        # It replaces the confidence gate only; every other gate and check below still applies.
        if isinstance(approval, ProposalStop):
            raise approval
        if not _approval_matches(approval, report):
            raise ProposalStop(NOT_GENERATED, "LOW_CONFIDENCE",
                               f"confidence {parsed.confidence:.4f} < {min_confidence:.2f} "
                               f"and no recorded human approval for this diagnosis")
        routing = {"mode": HUMAN_APPROVED, "approval_required": True,
                   "decision_id": approval["decision_id"], "decided_at": approval.get("decided_at")}

    if telemetry.get("available") is not True:
        raise ProposalStop(NOT_GENERATED, "INSUFFICIENT_EVIDENCE", "no telemetry was collected")
    peak = summary.get(spec["peak_key"])
    if not (_is_real(peak) and peak > 0):
        raise ProposalStop(NOT_GENERATED, "INSUFFICIENT_EVIDENCE", f"no positive {spec['resource']} peak")
    if raw_tel.get(f"{spec['resource']}_available") is False:
        raise ProposalStop(NOT_GENERATED, "INSUFFICIENT_EVIDENCE",
                           f"raw context says {spec['resource']} telemetry was unavailable")

    retrieved = {h.get("runbook_id") for h in retrieved_hits if isinstance(h, dict)}
    runbook = next((d for d in load_runbooks() if d["runbook_id"] == spec["runbook"]), None)
    if (rag.get("status") != "ok" or spec["runbook"] not in retrieved or runbook is None
            or runbook.get("verified") is not True or runbook.get("source_status") != "curated"
            or runbook.get("source_type") != "official_documentation"):
        raise ProposalStop(NOT_GENERATED, "NO_SUPPORTING_RUNBOOK",
                           f"official runbook {spec['runbook']} was not retrieved for this incident")
    return {"report": report, "parsed": parsed, "action": action, "spec": spec,
            "peak": float(peak), "runbook": runbook, "labels": labels, "alert": alert,
            "rule_action": rule_action, "routing": routing,
            "rule_class": _plain_label(rule_class) or ("(withheld)" if rule_class else None)}


def report_sha256(report) -> str:
    """Fingerprint of a stored diagnosis report (the 'diagnosis version')."""
    return _sha256_text(_canonical_json(report))


def _approval_matches(approval, report) -> bool:
    """True only for an APPROVED, approval-required routing row for exactly this report."""
    return (isinstance(approval, dict) and approval.get("decision") == APPROVED
            and approval.get("route") == HUMAN_APPROVAL_REQUIRED and bool(approval.get("decision_id"))
            and approval.get("report_sha256") == report_sha256(report))


def parse_github_repo(value) -> Optional[str]:
    """'owner/repo' (lower-case) for a GitHub repo identifier, or None if it is not one.

    Accepted: 'owner/repo', 'https://github.com/owner/repo[.git]', 'ssh://git@github.com/owner/repo[.git]'
    and 'git@github.com:owner/repo[.git]'. The host must be exactly github.com; any other host,
    scheme, port, query, fragment or extra path segment is rejected.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 300 or any(ch.isspace() for ch in value):
        return None
    scp = re.fullmatch(r"git@github\.com:([^/]+)/([^/]+)", value)
    if scp:
        owner, repo = scp.groups()
    elif "://" in value:
        try:
            url = urllib.parse.urlsplit(value)
            port = url.port
        except ValueError:
            return None
        if (url.scheme not in ("https", "ssh") or url.hostname != "github.com" or port is not None
                or url.query or url.fragment or (url.scheme == "ssh" and url.username != "git")):
            return None
        parts = url.path.split("/")
        if parts[-1] == "":
            parts = parts[:-1]
        if len(parts) != 3 or parts[0] != "":
            return None
        owner, repo = parts[1], parts[2]
    else:
        parts = value.split("/")
        if len(parts) != 2:
            return None
        owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    for part in (owner, repo):
        if not _REPO_PART_RE.fullmatch(part) or part in (".", ".."):
            return None
    return f"{owner}/{repo}".lower()


def local_origin(repo_root) -> Optional[str]:
    """owner/repo of the checkout's 'origin' remote, read from .git/config (no subprocess)."""
    config = Path(repo_root) / ".git" / "config"
    try:
        if not config.is_file() or config.stat().st_size > 64 * 1024:
            return None
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = re.search(r'\[remote "origin"\]([^\[]*)', text)
    url = re.search(r"^[ \t]*url[ \t]*=[ \t]*(\S+)", section.group(1), re.MULTILINE) if section else None
    return parse_github_repo(url.group(1)) if url else None


def _rel_target(cfg: dict, env) -> str:
    return (cfg.get("target_manifest_path") or env.get("TARGET_MANIFEST_PATH") or TARGET_MANIFEST).strip()


def candidate_values(spec: dict, current: str, request: Optional[str], effective_factor: float,
                     rule_value: Optional[str]) -> List[str]:
    parse, fmt = spec["parse"], spec["format"]
    cur = parse(current)
    cap = parse(HARD_CAPS[spec["resource"]])
    req = parse(request) if request else 0
    upper = int(cur * effective_factor)   # the same cap PatchGenerator applies
    raw = [cur * m for m in CANDIDATE_MULTIPLIERS if m <= effective_factor + 1e-9]
    if spec["resource"] == "memory":
        # format_memory() writes whole Gi at or above 1Gi, so round down to a value it can express.
        values = [int(v // unit) * unit for v in raw for unit in [_MIB * 1024 if v >= _MIB * 1024 else _MIB]]
    else:
        values = [int(v) for v in raw]
    if rule_value:
        try:
            values.append(parse(rule_value))
        except (ValueError, TypeError, OverflowError):
            pass
    out = []
    for value in sorted(set(values)):
        text = fmt(value)
        if (value > cur and value <= upper and value <= cap and value >= req
                and parse(text) == value and fmt(parse(text)) == text and text not in out):
            out.append(text)
    return out


def _numbers(text: str, ignore=()) -> set:
    """Numbers in text, after removing identifiers that contain digits (fact/runbook ids, "k8s")."""
    text = text or ""
    for token in (*ignore, "k8s"):
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", " ", text, flags=re.IGNORECASE)
    return {round(float(n), 4) for n in _NUMBER_RE.findall(text)}


# ─── LLM (constrained choice only) ───

def build_decision_model(fact_ids: List[str], runbook_ids: List[str], candidates: List[str]):
    fields = {
        "fact_ids": (List[Literal[tuple(fact_ids)]], Field(min_length=1, max_length=6)),
        "runbook_ids": (List[Literal[tuple(runbook_ids)]], Field(max_length=2)),
        "rationale": (str, Field(min_length=1, max_length=400)),
        "decision": (Literal["PROPOSE", "DECLINE"], ...),
        "new_value": (Literal[tuple(candidates + ["NONE"])], ...),
        "decline_reason": (Literal[DECLINE_REASONS], ...),
    }
    return create_model("PatchDecision", __config__=ConfigDict(extra="forbid"), **fields)


def build_proposer_llm(schema: dict, settings: Settings):
    """Same provider/model resolution as diagnosis (build_llm), plus a request timeout."""
    base, provider, model_name = build_llm(output_schema=schema)
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        llm = ChatOllama(
            model=base.model, base_url=base.base_url, num_ctx=base.num_ctx,
            num_predict=settings.llm_max_tokens, temperature=0.0, format=schema,
            client_kwargs={"timeout": settings.llm_timeout_s},
        )
        options = {"num_ctx": base.num_ctx, "num_predict": settings.llm_max_tokens}
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=model_name, temperature=0.0, timeout=settings.llm_timeout_s,
                         max_retries=0, max_tokens=settings.llm_max_tokens)
        options = {"max_tokens": settings.llm_max_tokens}
    else:  # build_llm already rejects unknown providers
        raise ValueError(f"unsupported provider {provider}")
    options.update({"temperature": 0.0, "timeout_s": settings.llm_timeout_s})
    return llm, provider, model_name, options


def invoke_with_guard(llm, prompt: str, guard_s: float):
    """Runs llm.invoke in a daemon thread so a hung call can never block exit."""
    box = {}

    def run():
        try:
            box["out"] = llm.invoke(prompt)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["err"] = exc

    thread = threading.Thread(target=run, name="patch-proposer-llm", daemon=True)
    thread.start()
    thread.join(guard_s)
    if thread.is_alive():
        raise _GuardTimeout(f"no response within {guard_s:.0f}s")
    if "err" in box:
        raise box["err"]
    return box.get("out")


def classify_llm_error(exc: BaseException) -> str:
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & {"ConnectTimeout", "PoolTimeout"}:
        return "LLM_UNAVAILABLE"   # never connected: the server is not reachable
    if isinstance(exc, (_GuardTimeout, TimeoutError)) or any("Timeout" in n for n in names):
        return "LLM_TIMEOUT"
    if names & {"ConnectError", "ConnectionError", "APIConnectionError", "NetworkError", "RemoteProtocolError"}:
        return "LLM_UNAVAILABLE"
    if getattr(exc, "status_code", None) in (401, 403, 404):
        return "LLM_UNAVAILABLE"   # Ollama model not pulled, OpenAI model missing or bad key
    if "OpenAIError" in names and ("api_key" in str(exc).lower() or "credentials" in str(exc).lower()):
        return "LLM_UNAVAILABLE"   # no OpenAI key configured
    return "LLM_ERROR"


def _build_prompt(ctx: dict) -> str:
    facts = "\n".join(f"{f['id']}: {f['text']}" for f in ctx["facts"])
    guidance = "\n".join(f"- {g}" for g in ctx["guidance"])
    skeleton = json.dumps({
        "fact_ids": ["F3"], "runbook_ids": [ctx["runbook_id"]], "rationale": "two short sentences",
        "decision": "PROPOSE or DECLINE", "new_value": "one MENU value or NONE",
        "decline_reason": "NONE or a reason",
    })
    return f"""You are the AutoFixOps patch proposer. You only CHOOSE; code writes and validates any
change. Nothing is applied, committed or deployed, and a human reviews the result later.

TASK: The diagnosis recommends {ctx['action']} for container "{ctx['container']}". Choose ONE value
from MENU for its {ctx['resource']} limit, or DECLINE.

FACTS (from the incident and the manifest; cite by id):
{facts}

DIAGNOSIS (inference, not evidence): {ctx['diagnosis']}
NOTE: {ctx['rule_note']}

GUIDANCE (official documentation, {ctx['runbook_source']}; runbook_id {ctx['runbook_id']}):
{guidance}

MENU for the {ctx['resource']} limit: {', '.join(ctx['candidates'])}

RULES:
1. PROPOSE only if the telemetry facts ({', '.join(TELEMETRY_FACT_IDS)}) show the workload needs more
   {ctx['resource']}; cite at least one of them. Prefer the smallest value that fits.
2. If the evidence suggests a leak or does not show a capacity problem, DECLINE.
3. Use only numbers that appear in FACTS or MENU. Do not claim that anything happened before.
   Write the rationale in plain ASCII text.
4. Reply with one JSON object only, for example: {skeleton}
"""


# ─── The proposal (pure: no database) ───

def _empty_record(snap: dict) -> dict:
    return {
        "status": FAILED, "reason_code": "INTERNAL_ERROR", "reason_detail": None,
        "action_type": None, "target_file": None, "target_ref": None, "base": None,
        "patched_git_blob_sha": None, "previous_values": None, "proposed_values": None,
        "candidates": None, "unified_diff": None, "diff_sha256": None, "evidence": None,
        "llm_decision": None, "rationale": None, "review_notes": [], "validation": {"steps": []},
        "model_info": {}, "source_report": None, "decision": None, "llm_calls": 0,
        "sandbox_path": None,
    }


def _source_report(report: dict) -> dict:
    """Summary of the diagnosis the proposal is based on; sha256 identifies the exact report."""
    proposed = _obj(report.get("proposed_action"))
    action_type = proposed.get("type")
    scalars = {}
    for key in ("generated_at", "report_version", "model", "llm_provider", "incident_type", "confidence"):
        value = report.get(key)
        scalars[key] = value if value is None or isinstance(value, (str, bool)) or _is_real(value) else "(invalid)"
    level, rag_status = _obj(report.get("risk_impact")).get("level"), _obj(report.get("rag")).get("status")
    return {
        "sha256": _sha256_text(_canonical_json(report)), **scalars,
        "proposed_action_type": action_type if isinstance(action_type, str) and _ACTION_TYPE_RE.fullmatch(action_type) else None,
        "risk_level": level if isinstance(level, str) else None,
        "rag_status": rag_status if isinstance(rag_status, str) else None,
    }


def propose(snap: dict, cfg: dict, system_mode: str, repo_root, *, llm=None, settings: Settings = None,
            env=None, preset_stop: Optional[ProposalStop] = None, monotonic=time.monotonic,
            mode_check: Optional[Callable[[], str]] = None, approval=None) -> dict:
    """Runs gates, the constrained LLM choice and validation. Never touches the database.

    mode_check, if given, re-reads the kill switch immediately before the LLM call.
    approval is the stored routing row for this report (read by the caller from the
    database, never from a client), a ProposalStop if it could not be read, or None.
    """
    env = os.environ if env is None else env
    rec = _empty_record(snap)
    steps = _Steps()
    rec["validation"]["steps"] = steps.items
    try:
        report = snap.get("ai_report")
        if isinstance(report, dict):
            rec["source_report"] = _source_report(report)
            rec["action_type"] = rec["source_report"]["proposed_action_type"]
        # The kill switch wins over every other stop reason, including configuration errors.
        if system_mode == "DISABLED":
            raise ProposalStop(NOT_GENERATED, "SYSTEM_DISABLED", "kill switch is DISABLED")
        if preset_stop is not None:
            raise preset_stop
        if settings is None:
            try:
                settings = settings_from_env(env)
            except (TypeError, ValueError) as exc:
                raise ProposalStop(NOT_GENERATED, "CONFIG_INVALID", str(exc))
        conflict = threshold_conflict(cfg.get("confidence_threshold"), "project confidence_threshold")
        if conflict:
            raise ProposalStop(NOT_GENERATED, "CONFIG_INVALID", conflict)
        _run(snap, cfg, system_mode, Path(repo_root), llm, settings, env, rec, steps, monotonic, mode_check,
             approval)
    except ProposalStop as stop:
        rec.update(status=stop.status, reason_code=stop.code, reason_detail=_one_line(stop.detail, 1000))
    except Exception as exc:  # noqa: BLE001 - never a partial success
        rec.update(status=FAILED, reason_code="INTERNAL_ERROR",
                   reason_detail=_one_line(f"{type(exc).__name__}: {exc}", 300))
    enforce_invariants(rec)
    try:
        json.dumps({c: rec.get(c) for c in _COLUMNS}, allow_nan=False)
    except (TypeError, ValueError) as exc:
        rec.update(status=FAILED, reason_code="INTERNAL_ERROR",
                   reason_detail=f"proposal record is not storable as JSON: {type(exc).__name__}",
                   target_ref=None, candidates=None, evidence=None, llm_decision=None, source_report=None,
                   model_info={}, validation={"steps": []}, review_notes=[])
        enforce_invariants(rec)
    return rec


def _assert_finite_numbers(node, depth=0):
    """Rejects .nan/.inf anywhere in the manifest (they break the before/after comparison)."""
    if depth > 100:
        raise PatchDiffError("UNSUPPORTED_MANIFEST", "YAML nesting is too deep")
    if isinstance(node, float) and not math.isfinite(node):
        raise PatchDiffError("UNSUPPORTED_MANIFEST", "non-finite numbers (.nan/.inf) are not supported")
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        _assert_finite_numbers(child, depth + 1)


def _gate(steps: _Steps, check: str, fn, status_on_fail=NOT_GENERATED):
    t0 = time.monotonic()
    try:
        result = fn()
    except (PatchSafetyError, PatchDiffError) as exc:
        steps.add(check, "FAILED", exc.code, exc.detail, t0)
        raise ProposalStop(status_on_fail, exc.code, exc.detail)
    except ProposalStop as stop:
        steps.add(check, "FAILED", stop.code, stop.detail, t0)
        raise
    steps.add(check, "PASSED", t0=t0)
    return result


def _run(snap, cfg, system_mode, repo_root: Path, llm, settings: Settings, env, rec, steps, monotonic,
         mode_check=None, approval=None):
    g = _report_gates(snap, settings, system_mode, approval)
    spec, action, report = g["spec"], g["action"], g["report"]
    rec["source_report"]["routing"] = g["routing"]
    steps.add("confidence_route", "PASSED", detail=(
        f"confidence {g['parsed'].confidence:.4f} >= {CONFIDENCE_FLOOR:.2f}: automatic AI route"
        if g["routing"]["mode"] == AUTOMATIC_AI else
        f"confidence {g['parsed'].confidence:.4f} < {CONFIDENCE_FLOOR:.2f}: human approval recorded"))
    resource = spec["resource"]
    field_path = ("resources", "limits", resource)

    # V1-V5: target and configuration gates (before any LLM call).
    rel = _gate(steps, "path_syntax", lambda: validate_repo_relative_path(_rel_target(cfg, env)))
    rec["target_file"] = rel
    file_spec, _ = _gate(steps, "path_allowlist", lambda: allowlisted_field(rel, action))
    read = _gate(steps, "path_containment", lambda: read_allowlisted_file(repo_root, rel))
    rec["base"] = read.base_record()

    def manifest_shape():
        if not read.text_lf.endswith("\n"):
            raise PatchDiffError("UNSUPPORTED_MANIFEST", "file must end with a newline")
        # Event-level checks first: anchors/aliases are refused before anything expands them
        # (an alias "billion laughs" manifest would otherwise be expanded by safe_load_all).
        assert_plain_document(read.text_lf)
        try:
            docs = [d for d in yaml.safe_load_all(read.text_lf)]
        except (yaml.YAMLError, RecursionError) as exc:
            raise PatchDiffError("UNSUPPORTED_MANIFEST", f"YAML error: {_one_line(exc, 200)}")
        except (ValueError, TypeError, OverflowError) as exc:
            # The YAML constructors raise these for values like 2026-13-45 or a 5000-digit integer.
            raise PatchDiffError("UNSUPPORTED_MANIFEST", f"unsupported YAML value ({type(exc).__name__})")
        if len(docs) != 1 or not isinstance(docs[0], dict):
            raise PatchDiffError("UNSUPPORTED_MANIFEST", "exactly one YAML mapping document is required")
        doc = docs[0]
        kind = doc.get("kind")
        if doc.get("apiVersion") != "apps/v1" or not isinstance(kind, str) or kind not in file_spec["kinds"]:
            raise PatchDiffError("UNSUPPORTED_MANIFEST", "only an apps/v1 Deployment is supported")
        meta = doc.get("metadata")
        if not isinstance(meta, dict) or _plain_label(meta.get("name")) is None:
            raise PatchDiffError("UNSUPPORTED_MANIFEST", "metadata.name must be a plain string")
        if meta.get("namespace") is not None and _plain_label(meta.get("namespace")) is None:
            raise PatchDiffError("UNSUPPORTED_MANIFEST", "metadata.namespace must be a plain string")
        _assert_finite_numbers(doc)
        span = locate_container_scalar(read.text_lf, field_path)
        try:
            request_span = locate_container_scalar(read.text_lf, ("resources", "requests", resource))
        except PatchDiffError as exc:
            if exc.code != "CURRENT_VALUE_MISSING":
                raise
            request_span = None
        current = span["value"]
        try:
            if spec["format"](spec["parse"](current)) != current or spec["parse"](current) <= 0:
                raise ValueError
            if request_span:
                spec["parse"](request_span["value"])
        except (ValueError, TypeError, OverflowError):
            raise PatchDiffError("UNSUPPORTED_MANIFEST", f"non-canonical {resource} value")
        loaded = ((doc["spec"]["template"]["spec"]["containers"][0].get("resources") or {}).get("limits") or {})
        if loaded.get(resource) != current:
            raise PatchDiffError("UNSUPPORTED_MANIFEST", f"the {resource} limit must be a string (quote it)")
        return doc, span, (request_span or {}).get("value")

    doc, span, request = _gate(steps, "manifest_shape", manifest_shape)
    meta = doc.get("metadata") or {}
    container = doc["spec"]["template"]["spec"]["containers"][0]
    name, namespace, cname = meta.get("name"), meta.get("namespace"), container.get("name")

    def identity_and_config():
        labels = g["labels"]
        for key in ("namespace", "deployment", "pod", "container"):
            value = labels.get(key)
            if value is not None and _plain_label(value) is None:
                raise ProposalStop(NOT_GENERATED, "TARGET_MISMATCH", f"alert label {key} is not a plain value")
        if labels.get("namespace") and labels["namespace"] != namespace:
            raise ProposalStop(NOT_GENERATED, "TARGET_MISMATCH", "alert namespace differs from the manifest")
        if cfg.get("target_namespace") and cfg["target_namespace"] != namespace:
            raise ProposalStop(NOT_GENERATED, "TARGET_MISMATCH", "manifest namespace differs from the project")
        if namespace in RESTRICTED_NAMESPACES or labels.get("namespace") in RESTRICTED_NAMESPACES:
            raise ProposalStop(NOT_GENERATED, "RESTRICTED_NAMESPACE", f"namespace {namespace} is restricted")
        if labels.get("deployment") and labels["deployment"] != name:
            raise ProposalStop(NOT_GENERATED, "TARGET_MISMATCH", "alert deployment differs from the manifest")
        pod = labels.get("pod")
        if pod and pod != name and not pod.startswith(f"{name}-"):
            raise ProposalStop(NOT_GENERATED, "TARGET_MISMATCH", "alert pod does not belong to the deployment")
        if labels.get("container") and labels["container"] != cname:
            raise ProposalStop(NOT_GENERATED, "TARGET_MISMATCH", "alert container differs from the manifest")
        if _plain_label(cname) is None:
            raise ProposalStop(NOT_GENERATED, "TARGET_MISMATCH", "manifest container name is not a plain value")
        configured_raw = cfg.get("github_repo") or env.get("GITHUB_REPO") or ""
        configured = configured_raw.strip() if isinstance(configured_raw, str) else "(invalid)"
        local = local_origin(repo_root)
        if configured:
            wanted = parse_github_repo(configured)
            if wanted is None:
                raise ProposalStop(NOT_GENERATED, "REPO_MISMATCH",
                                   "configured GitHub repo is not a valid github.com owner/repo")
            if local is None:
                raise ProposalStop(NOT_GENERATED, "REPO_MISMATCH",
                                   "this checkout has no github.com 'origin' remote to compare with")
            if wanted != local:
                raise ProposalStop(NOT_GENERATED, "REPO_MISMATCH",
                                   "configured GitHub repo is not this checkout's origin")
            configured = wanted
        raw_factor = cfg.get("max_resource_scale_factor")
        try:
            factor = POLICY_MAX_FACTOR if raw_factor is None else float(raw_factor)
        except (TypeError, ValueError):
            raise ProposalStop(NOT_GENERATED, "CONFIG_INVALID", "max_resource_scale_factor is not a number")
        effective = min(factor, settings.max_factor, POLICY_MAX_FACTOR)
        if not math.isfinite(effective) or effective <= 1.0:
            raise ProposalStop(NOT_GENERATED, "CONFIG_INVALID", f"effective scale factor {effective} <= 1.0")
        limit_amount = spec["parse"](span["value"])
        peak_amount = g["peak"] * (1000.0 if resource == "cpu" else 1.0)
        utilisation = peak_amount / limit_amount
        min_utilisation = _at_least(settings.min_utilisation, POLICY_MIN_UTILISATION)
        if not (_is_real(utilisation) and utilisation >= min_utilisation):
            raise ProposalStop(NOT_GENERATED, "INSUFFICIENT_EVIDENCE",
                               f"peak is {utilisation:.0%} of the limit (< {min_utilisation:.0%})")
        if not utilisation <= MAX_PLAUSIBLE_UTILISATION:
            raise ProposalStop(NOT_GENERATED, "INSUFFICIENT_EVIDENCE",
                               f"peak is {utilisation:.0%} of the limit; telemetry does not match this manifest")
        rule_action = g["rule_action"]
        rule_value = rule_action.get("patch_value") if rule_action.get("type") == action else None
        if not isinstance(rule_value, str):
            rule_value = None
        values = candidate_values(spec, span["value"], request, effective, rule_value)
        if span["style"] is None:   # plain scalar: keep only values that still load as the same string
            values = [v for v in values if yaml.safe_load(v) == v]
        if not values:
            raise ProposalStop(NOT_GENERATED, "AT_BOUND", "no allowed value above the current limit")
        return {"configured_repo": configured, "local_origin": local, "effective": effective,
                "utilisation": utilisation, "candidates": values, "rule_value": rule_value}

    ic = _gate(steps, "identity_and_config", identity_and_config)
    rec["target_ref"] = {
        "api_version": doc.get("apiVersion"), "kind": doc.get("kind"), "name": name,
        "namespace": namespace, "container": cname,
        "field_path": f"spec.template.spec.containers[0].resources.limits.{resource}",
        "line": span["line"], "column": span["column"],
    }
    rec["candidates"] = {
        "values": ic["candidates"], "effective_factor": ic["effective"],
        "hard_cap": HARD_CAPS[resource], "current_value": span["value"], "request_value": request,
        "rule_engine_patch_value": ic["rule_value"],
    }

    # Facts and guidance offered to the LLM (code-derived only).
    peak = g["peak"]
    peak_text = f"{peak / _MIB:.1f}Mi" if resource == "memory" else f"{peak * 1000:.0f}m"
    alert = g["alert"]
    alert_name = _plain_label(alert.get("name")) or "(withheld)"
    severity = _plain_label(alert.get("severity")) or "unknown"
    facts = [
        {"id": "F1", "source": "alert", "text": f"alert {alert_name}, severity {severity}"},
        {"id": "F2", "source": "telemetry", "text": "telemetry was collected for this incident"},
        {"id": "F3", "source": "telemetry", "text": f"{resource} peak {peak_text}"},
        {"id": "F4", "source": "code", "text": f"peak is {ic['utilisation'] * 100:.0f}% of the current {resource} limit"},
        {"id": "M1", "source": "manifest", "text": f"current {resource} limit {span['value']}"},
    ]
    if request:
        facts.append({"id": "M2", "source": "manifest", "text": f"current {resource} request {request}"})
    facts.append({"id": "D1", "source": "diagnosis",
                  "text": f"diagnosis incident_type {g['parsed'].incident_type}, confidence {g['parsed'].confidence:.2f}"})
    runbook = g["runbook"]
    guidance = [str(x)[:220] for x in (runbook["sections"].get("remediation_considerations") or [])[:3]]
    guidance += [str(x)[:220] for x in (runbook["sections"].get("safety_limitations") or [])[:2]]
    rule_note = (f"rule-engine category label {g['rule_class']} is a static rule keyed on the alert name; "
                 f"it is not evidence of a leak" if g["rule_class"] else "no rule-engine label")
    ctx = {
        "action": action, "container": cname, "resource": resource, "facts": facts, "guidance": guidance,
        "diagnosis": _one_line(g["parsed"].root_cause_analysis, 300), "rule_note": rule_note,
        "runbook_id": runbook["runbook_id"],
        "runbook_source": f"{runbook['source_organization']} - {runbook['source_title']}",
        "candidates": ic["candidates"],
    }
    fact_ids = [f["id"] for f in facts]
    rec["evidence"] = {
        "facts_offered": facts, "fact_ids_cited": [],
        "runbooks_offered": [{"runbook_id": runbook["runbook_id"], "guidance_source": "code",
                              "guidance_version": RUNBOOK_VERSION, "source_url": runbook["source_url"],
                              "source": ctx["runbook_source"], "guidance": guidance}],
        "runbook_ids_cited": [],
    }
    model_cls = build_decision_model(fact_ids, [runbook["runbook_id"]], ic["candidates"])
    schema = model_cls.model_json_schema()
    prompt = _build_prompt(ctx)
    # F1 carries the report-controlled alert name, so its digits do not count as evidence.
    grounded_numbers = _numbers("\n".join(f["text"] for f in facts if f["id"] != "F1") + " "
                                + " ".join(ic["candidates"]) + " " + " ".join(guidance))
    info = rec["model_info"]
    info.update({"prompt_version": PROMPT_VERSION, "prompt_sha256": _sha256_text(prompt),
                 "prompt_chars": len(prompt)})

    # The single LLM call: choose a value or decline.
    t0 = time.monotonic()

    def llm_failed(exc, code):
        info.update({"latency_ms": int((time.monotonic() - t0) * 1000), "error_class": type(exc).__name__,
                     "error_message": _one_line(exc, 300)})
        steps.add("llm_decision", "FAILED", code, type(exc).__name__, t0)
        raise ProposalStop(FAILED, code, f"{type(exc).__name__}: {_one_line(exc, 200)}")

    if llm is None:
        try:
            client, provider, model_name, options = build_proposer_llm(schema, settings)
        except ValueError as exc:   # unknown LLM_PROVIDER, bad OLLAMA_NUM_CTX, ...
            steps.add("llm_decision", "FAILED", "CONFIG_INVALID", type(exc).__name__, t0)
            raise ProposalStop(NOT_GENERATED, "CONFIG_INVALID", f"LLM configuration: {_one_line(exc, 200)}")
        except Exception as exc:  # noqa: BLE001 - e.g. no OpenAI key -> LLM_UNAVAILABLE
            llm_failed(exc, classify_llm_error(exc))
    else:
        client, provider, model_name, options = llm, "injected", getattr(llm, "model_name", "injected"), {}
    info.update({"provider": provider, "model": model_name, **options})
    if mode_check is not None:
        # Last kill-switch read before anything is sent to the model.
        try:
            mode_now = mode_check()
        except Exception as exc:  # noqa: BLE001 - cannot show the switch is still on
            raise ProposalStop(FAILED, "CONFIG_UNAVAILABLE", f"kill switch re-read failed: {_error_summary(exc)}")
        if mode_now == "DISABLED":
            raise ProposalStop(NOT_GENERATED, "SYSTEM_DISABLED", "kill switch was set to DISABLED before the LLM call")
    try:
        rec["llm_calls"] += 1
        out = invoke_with_guard(client, prompt, settings.guard_s)
    except Exception as exc:  # noqa: BLE001 - classified below
        llm_failed(exc, classify_llm_error(exc))
    info["latency_ms"] = int((time.monotonic() - t0) * 1000)

    raw = out.content if hasattr(out, "content") else out

    def reject(code, detail):
        text = raw if isinstance(raw, str) else repr(raw)
        # A short one-line excerpt is enough to diagnose a bad answer; the full text is not kept.
        info.update({"raw_output_excerpt": _one_line(text, 300), "raw_output_chars": len(text),
                     "raw_output_sha256": _sha256_text(text)})
        steps.add("llm_decision", "FAILED", code, detail, t0)
        raise ProposalStop(REJECTED, code, detail)

    if not isinstance(raw, str):
        reject("LLM_MALFORMED_OUTPUT", "model output is not text")
    try:
        decision = model_cls.model_validate_json(raw.strip())
    except ValidationError as exc:
        errors = exc.errors()
        types = {err.get("type") for err in errors}
        choice_only = bool(errors) and all(err.get("type") == "literal_error" and isinstance(err.get("input"), str)
                                           for err in errors)
        code = "LLM_INVALID_CHOICE" if choice_only else "LLM_MALFORMED_OUTPUT"
        reject(code, f"output failed schema validation ({', '.join(sorted(t for t in types if t))})")
    d = decision.model_dump()
    if d["decision"] == "PROPOSE" and (d["new_value"] == "NONE" or d["decline_reason"] != "NONE"):
        reject("LLM_INVALID_CHOICE", "PROPOSE must name a value and no decline reason")
    if d["decision"] == "DECLINE" and (d["new_value"] != "NONE" or d["decline_reason"] == "NONE"):
        reject("LLM_INVALID_CHOICE", "DECLINE must name a reason and no value")
    if any(unicodedata.category(ch) in _UNSAFE_CATEGORIES for ch in d["rationale"]):
        reject("LLM_MALFORMED_OUTPUT", "rationale contains control or invisible formatting characters")
    # NFKC folds compatibility forms (full-width digits, ligatures) before the best-effort filters.
    folded = unicodedata.normalize("NFKC", d["rationale"])
    if not folded.isascii():
        reject("LLM_MALFORMED_OUTPUT", "rationale must be plain ASCII text")
    for pattern in (*_HISTORICAL_CLAIM_PATTERNS, _PROPOSER_HISTORY_RE):
        if pattern.search(folded):
            reject("UNGROUNDED_CLAIM", "rationale claims a historical outcome")
    ungrounded = _numbers(folded, ignore=fact_ids + [runbook["runbook_id"]]) - grounded_numbers
    if ungrounded:
        reject("UNGROUNDED_CLAIM", f"rationale uses numbers not in FACTS or MENU: {sorted(ungrounded)}")
    rec["llm_decision"] = d
    rec["decision"] = d["decision"]
    rec["rationale"] = d["rationale"]
    rec["evidence"]["fact_ids_cited"] = d["fact_ids"]
    rec["evidence"]["runbook_ids_cited"] = d["runbook_ids"]
    if d["decision"] == "DECLINE":
        steps.add("llm_decision", "PASSED", "MODEL_DECLINED", d["decline_reason"], t0)
        raise ProposalStop(NOT_GENERATED, "MODEL_DECLINED", f"model declined: {d['decline_reason']}")
    if not set(d["fact_ids"]) & set(TELEMETRY_FACT_IDS):
        steps.add("llm_decision", "FAILED", "NO_EVIDENCE_CITED", "no telemetry fact cited", t0)
        raise ProposalStop(NOT_GENERATED, "NO_EVIDENCE_CITED", "PROPOSE did not cite a telemetry fact")
    steps.add("llm_decision", "PASSED", t0=t0)

    # V6-V13: validate the code-built edit and diff as untrusted input.
    deadline = _Deadline(settings.validation_timeout_s, monotonic)
    new_value = d["new_value"]
    old_amount, new_amount = spec["parse"](span["value"]), spec["parse"](new_value)
    before_lf = read.text_lf

    def bounds():
        cap = spec["parse"](HARD_CAPS[resource])
        req = spec["parse"](request) if request else 0
        if not (new_amount > old_amount and new_amount <= int(old_amount * ic["effective"])
                and new_amount <= cap and new_amount >= req and spec["format"](new_amount) == new_value
                and new_value in ic["candidates"]):
            raise ProposalStop(REJECTED, "BOUNDS_VIOLATION", f"{new_value} is outside the allowed bounds")

    deadline.check("bounds")
    _gate(steps, "bounds", bounds, REJECTED)

    def render_textual():
        after = splice_scalar(before_lf, span, new_value)
        rendered = render_scalar(new_value, span["style"])
        b_lines, a_lines = before_lf.split("\n"), after.split("\n")
        diffs = [i for i, (x, y) in enumerate(zip(b_lines, a_lines)) if x != y]
        line_start = before_lf.rfind("\n", 0, span["start"]) + 1
        line_end = before_lf.find("\n", span["end"])
        prefix_ok = after[line_start:span["start"]] == before_lf[line_start:span["start"]]
        suffix_ok = (after[span["start"] + len(rendered):line_end + len(rendered) - (span["end"] - span["start"])]
                     == before_lf[span["end"]:line_end])
        if (len(b_lines) != len(a_lines) or diffs != [span["line"] - 1] or not prefix_ok or not suffix_ok
                or "\r" in after or not after.endswith("\n")):
            raise PatchDiffError("RENDER_FAILED", "edit is not a single in-place scalar replacement")
        return after

    deadline.check("render_textual")
    after_lf = _gate(steps, "render_textual", render_textual, REJECTED)
    diff_text = make_unified_diff(rel, before_lf, after_lf)

    def diff_policy():
        parsed = parse_unified_diff_strict(diff_text, rel)
        check_diff_policy(parsed, before_lf.split("\n"), span["line"], resource)

    def sandbox():
        result = sandbox_apply(rel, before_lf, diff_text, after_lf, repo_root)
        rec["sandbox_path"] = result["sandbox"]
        return result["after_text"]

    try:
        deadline.check("diff_policy")
        _gate(steps, "diff_policy", diff_policy, REJECTED)
        deadline.check("sandbox_apply")
        sandbox_after = _gate(steps, "sandbox_apply", sandbox, REJECTED)
    except ProposalStop as stop:
        if stop.status == REJECTED:
            # A rejected candidate diff is identified by hash and size only; its text is never stored.
            rec["validation"]["rejected_candidate"] = {
                "diff_sha256": _sha256_text(diff_text), "bytes": len(diff_text.encode("utf-8")), "valid": False}
        raise
    before_doc = yaml.safe_load(before_lf)
    expected_path = f"spec.template.spec.containers.[0].resources.limits.{resource}"

    def post_apply_yaml():
        try:
            docs = list(yaml.safe_load_all(sandbox_after))
        except yaml.YAMLError as exc:
            raise ProposalStop(REJECTED, "YAML_INVALID", f"patched YAML does not parse: {exc}")
        if len(docs) != 1 or not isinstance(docs[0], dict):
            raise ProposalStop(REJECTED, "YAML_INVALID", "patched YAML is not one mapping document")
        after_doc = docs[0]
        same_identity = (after_doc.get("apiVersion"), after_doc.get("kind"), after_doc.get("metadata")) == (
            before_doc.get("apiVersion"), before_doc.get("kind"), before_doc.get("metadata"))
        changed = changed_paths(before_doc, after_doc)
        if not same_identity or changed != {expected_path}:
            raise ProposalStop(REJECTED, "OUT_OF_SCOPE_CHANGE", f"changed paths: {sorted(changed)[:5]}")
        res = after_doc["spec"]["template"]["spec"]["containers"][0].get("resources") or {}
        new_limit = spec["parse"](str((res.get("limits") or {}).get(resource)))
        req = (res.get("requests") or {}).get(resource)
        if req is not None and new_limit < spec["parse"](str(req)):
            raise ProposalStop(REJECTED, "LIMIT_BELOW_REQUEST", "limit would be below the request")
        return after_doc

    deadline.check("post_apply_yaml")
    after_doc = _gate(steps, "post_apply_yaml", post_apply_yaml, REJECTED)

    def oracle():
        generator = PatchGenerator(max_scale_factor=ic["effective"])
        patched, previous = generator.generate({"type": action, "patch_value": new_value}, before_doc)
        expected_prev = {f"{resource}_limit_{cname}": span["value"]}
        if patched != after_doc or previous != expected_prev:
            raise ProposalStop(REJECTED, "CROSS_CHECK_MISMATCH", "PatchGenerator disagrees with the edit")
        if generator.apply_rollback(after_doc, previous) != before_doc:
            raise ProposalStop(REJECTED, "CROSS_CHECK_MISMATCH", "rollback does not restore the original")
        return previous

    deadline.check("patchgenerator_oracle")
    previous_values = _gate(steps, "patchgenerator_oracle", oracle, REJECTED)

    deadline.check("eol_variant")
    if read.eol == "CRLF":
        def eol_variant():
            raw_span = locate_container_scalar(read.text, field_path)
            raw_after = splice_scalar(read.text, raw_span, new_value)
            if raw_after.count("\r\n") != raw_after.count("\n") or raw_after.replace("\r\n", "\n") != after_lf:
                raise ProposalStop(REJECTED, "EOL_MISMATCH", "CRLF variant differs from the LF edit")
        _gate(steps, "eol_variant", eol_variant, REJECTED)
    else:
        steps.add("eol_variant", "SKIPPED", detail="base uses LF line endings")

    def real_tree_untouched():
        if rehash(repo_root, rel) != (read.sha256_raw, read.mtime_ns):
            raise ProposalStop(FAILED, "BASE_CHANGED", "the real target file changed during the run")

    deadline.check("real_tree_untouched")
    _gate(steps, "real_tree_untouched", real_tree_untouched, FAILED)
    deadline.check("completion")   # the last step must also finish inside the deadline

    # Everything passed: record the validated proposal.
    rendered = render_scalar(new_value, span["style"])
    rec.update(
        status=VALIDATED, reason_code=None, reason_detail=None,
        unified_diff=diff_text, diff_sha256=_sha256_text(diff_text),
        patched_git_blob_sha=git_blob_sha(after_lf.encode("utf-8")),
        previous_values=previous_values,
        proposed_values={"op": "replace_scalar", "field_path": rec["target_ref"]["field_path"],
                         "old_value": span["value"], "new_value": new_value,
                         "old_text": span["raw_slice"], "new_text": rendered},
    )
    line_text = before_lf.split("\n")[span["line"] - 1]
    notes = rec["review_notes"]
    if "#" in line_text[span["column"] - 1 + len(span["raw_slice"]):]:
        notes.append("edited line carries an inline comment that may now be inaccurate")
    if rec["source_report"] and rec["source_report"].get("risk_level") == "HIGH":
        notes.append("diagnosis risk_impact.level is HIGH")
    notes.append(f"runbook guidance comes from the code knowledge base (version {RUNBOOK_VERSION}); "
                 "the diagnosis-time runbook version is not recorded")
    notes.append(f"base is the local working tree ({read.eol}); Stage 3 must re-validate against the PR base "
                 "using base.git_blob_sha")
    if not ic["configured_repo"]:
        notes.append("no GitHub repo is configured; the base could not be tied to a remote")


def enforce_invariants(rec: dict, final: bool = False) -> dict:
    """A proposal is VALIDATED only with a PROPOSE decision, every mandatory check passed exactly
    once, no failed step anywhere, the patch details present and a diff whose hash matches.

    final=True (used by finalize) additionally requires the pre-commit re-check to have passed.
    """
    if rec["status"] == VALIDATED:
        steps = rec["validation"]["steps"]
        required = MANDATORY_CHECKS + (("precommit_recheck",) if final else ())
        by_check = {}
        for step in steps:
            by_check.setdefault(step["check"], []).append(step)

        def passed(check):
            entries = by_check.get(check, [])
            if len(entries) != 1:
                return False
            status = entries[0]["status"]
            return status == "PASSED" or (check == "eol_variant" and status == "SKIPPED"
                                          and (rec.get("base") or {}).get("eol") == "LF")

        diff = rec.get("unified_diff")
        ok = (rec.get("decision") == "PROPOSE"
              and all(passed(c) for c in required)
              and not any(s["status"] == "FAILED" for s in steps)
              and isinstance(diff, str) and diff and _sha256_text(diff) == rec.get("diff_sha256")
              and rec.get("proposed_values") and rec.get("previous_values") and rec.get("patched_git_blob_sha"))
        if not ok:
            rec.update(status=FAILED, reason_code="INTERNAL_INVARIANT",
                       reason_detail="VALIDATED without every check passed, a decision and a hashed diff")
    if rec["status"] != VALIDATED:
        rec["unified_diff"] = None
        rec["diff_sha256"] = None
        rec["patched_git_blob_sha"] = None
        rec["proposed_values"] = None
        rec["previous_values"] = None
    return rec


# ─── Persistence (two-phase, atomic start) ───

_COLUMNS = (
    "status", "reason_code", "reason_detail", "action_type", "target_file", "target_ref", "base",
    "patched_git_blob_sha", "previous_values", "proposed_values", "candidates", "unified_diff",
    "diff_sha256", "evidence", "llm_decision", "rationale", "review_notes", "validation",
    "model_info", "source_report",
)


def _utcnow() -> datetime.datetime:
    """Naive UTC (the columns are TIMESTAMP WITHOUT TIME ZONE)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# Outcomes in which the model was consulted and its answer was final for that diagnosis version.
# One of these per version, on every path: a new run would only re-roll the model.
MODEL_DECIDED_NOT_GENERATED = frozenset({"MODEL_DECLINED", "NO_EVIDENCE_CITED"})
# NOT_GENERATED outcomes caused by a transient condition (switch, configuration, approval state):
# the routing layer may retry them once the condition is gone.
RETRYABLE_NOT_GENERATED = frozenset({"SYSTEM_DISABLED", "CONFIG_INVALID", "APPROVAL_INVALID"})


class AlreadyGenerated(Exception):
    """The model already decided for the incident's current diagnosis version (see begin_run)."""

    def __init__(self, row):
        super().__init__(str(row.id))
        self.proposal_id, self.status, self.reason_code = row.id, row.status, row.reason_code


def _model_decided(row) -> bool:
    if row.status == VALIDATED:
        return _integrity_problem(row) is None
    return row.status == REJECTED or (row.status == NOT_GENERATED and row.reason_code in MODEL_DECIDED_NOT_GENERATED)


def begin_run(session_factory, incident_uuid, project_id, now=None):
    """Atomically refuses a concurrent run, then inserts a RUNNING row. Returns its id or None (busy).

    A RUNNING row younger than STALE_AFTER_S blocks a new run for the same incident, whoever
    started it. A proposal for the same report fingerprint in which the model already decided
    (VALIDATED, REJECTED, or a model decline) raises AlreadyGenerated: one model decision per
    diagnosis version on every path (worker, API retry, CLI), so the model cannot be re-rolled
    and no duplicate proposal is created. Gate-only stops and FAILED runs never block. On
    PostgreSQL the SELECT ... FOR UPDATE on the incident row serialises concurrent starts for the
    same incident; SQLite ignores the clause.
    """
    from api.models import Incident, PatchProposal

    now = now or _utcnow()
    db = session_factory()
    try:
        incident = db.query(Incident).filter(Incident.id == incident_uuid).with_for_update().first()
        if incident is None:
            db.rollback()
            raise LookupError("incident not found")
        cutoff = now - datetime.timedelta(seconds=STALE_AFTER_S)
        busy = (db.query(PatchProposal)
                .filter(PatchProposal.incident_id == incident_uuid, PatchProposal.status == RUNNING,
                        PatchProposal.created_at >= cutoff)
                .first())
        if busy is not None:
            db.rollback()
            return None
        # One model decision per diagnosis version, whoever asks (worker, API retry, CLI).
        sha = report_sha256(incident.ai_report) if isinstance(incident.ai_report, dict) else None
        if sha:
            decided = [row for row in (db.query(PatchProposal)
                                       .filter(PatchProposal.incident_id == incident_uuid,
                                               PatchProposal.status.in_((VALIDATED, REJECTED, NOT_GENERATED)))
                                       .order_by(PatchProposal.created_at.desc()).all())
                       if (row.source_report or {}).get("sha256") == sha and _model_decided(row)]
            if decided:
                db.rollback()
                raise AlreadyGenerated(decided[0])
        new_id = uuid.uuid4()
        db.add(PatchProposal(id=new_id, incident_id=incident_uuid,
                             project_id=uuid.UUID(project_id) if project_id else None,
                             status=RUNNING, proposer_version=PROPOSER_VERSION, created_at=now))
        db.commit()
        return new_id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _precommit_recheck(db, rec: dict, snap: dict):
    """Re-reads the incident (row-locked) and the kill switch in the writing transaction.

    A VALIDATED result is kept only if the incident is still DIAGNOSED by the AI engine with
    exactly the report the proposal was built from, in the same project, and the kill switch
    is not DISABLED. Otherwise the record becomes NOT_GENERATED and its diff is discarded.
    """
    from api.models import Incident

    t0 = time.monotonic()
    steps = _Steps(rec["validation"]["steps"])
    incident = (db.query(Incident).filter(Incident.id == uuid.UUID(snap["id"]))
                .with_for_update().populate_existing().first())
    mode = _system_mode(db)
    problem = None
    if mode == "DISABLED":
        problem = ("SYSTEM_DISABLED", "kill switch was set to DISABLED during the run")
    elif incident is None:
        problem = ("INCIDENT_CHANGED", "the incident no longer exists")
    else:
        status = incident.status.value if hasattr(incident.status, "value") else str(incident.status)
        report_sha = (_sha256_text(_canonical_json(incident.ai_report))
                      if isinstance(incident.ai_report, dict) else None)
        project = str(incident.project_id) if incident.project_id else None
        if status != "DIAGNOSED":
            problem = ("INCIDENT_CHANGED", f"incident status changed to {status} during the run")
        elif incident.diagnosed_by != "AI_ENGINE" or report_sha != (rec.get("source_report") or {}).get("sha256"):
            problem = ("INCIDENT_CHANGED", "the diagnosis report changed during the run")
        elif project != snap["project_id"]:
            problem = ("INCIDENT_CHANGED", "the incident's project changed during the run")
        else:
            # The route is re-derived from the stored report, not taken from the in-memory record:
            # below the floor, a matching APPROVED row must exist in this transaction.
            confidence = incident.ai_report.get("confidence")
            below_floor = not (_is_real(confidence) and confidence >= CONFIDENCE_FLOOR)
            routing = (rec.get("source_report") or {}).get("routing") or {}
            if below_floor or routing.get("mode") != AUTOMATIC_AI:
                approval = _read_approval(db, incident.id, report_sha)
                if not (routing.get("mode") == HUMAN_APPROVED and isinstance(approval, dict)
                        and approval.get("decision_id") == routing.get("decision_id")
                        and _approval_matches(approval, incident.ai_report)):
                    problem = ("APPROVAL_INVALID", "no recorded human approval matches this diagnosis")
    if problem:
        steps.add("precommit_recheck", "FAILED", problem[0], problem[1], t0)
        rec.update(status=NOT_GENERATED, reason_code=problem[0], reason_detail=problem[1])
    else:
        steps.add("precommit_recheck", "PASSED", t0=t0)


def _store_value(value):
    # Explicit SQL NULL: assigning None to a JSON column would store the JSON literal 'null'.
    return sa_null() if value is None else copy.deepcopy(value)


def finalize(session_factory, proposal_id, rec: dict, now=None, snap: Optional[dict] = None) -> bool:
    """Writes the terminal result, then reads it back. Returns False if it was not persisted.

    For a VALIDATED record, snap (the incident snapshot the run used) is required: the incident
    and kill switch are re-checked under a row lock in the same transaction as the write.
    """
    from api.models import PatchProposal

    now = now or _utcnow()
    try:
        db = session_factory()
        try:
            if rec["status"] == VALIDATED:
                if snap is None:
                    raise ValueError("a VALIDATED proposal needs the incident snapshot for the pre-commit re-check")
                _precommit_recheck(db, rec, snap)
            enforce_invariants(rec, final=True)
            row = db.query(PatchProposal).filter(PatchProposal.id == proposal_id).first()
            if row is None:
                raise LookupError("proposal row disappeared")
            for column in _COLUMNS:
                setattr(row, column, _store_value(rec.get(column)))
            row.completed_at = now
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        check = session_factory()
        try:
            stored = check.query(PatchProposal).filter(PatchProposal.id == proposal_id).first()
            if (stored is None or stored.status != rec["status"] or stored.diff_sha256 != rec["diff_sha256"]
                    or stored.unified_diff != rec["unified_diff"]
                    or (stored.unified_diff is not None
                        and _sha256_text(stored.unified_diff) != stored.diff_sha256)):
                raise RuntimeError("read-back does not match the written proposal")
        finally:
            check.close()
        return True
    except Exception as exc:  # noqa: BLE001 - reported as not persisted
        mark_failed_fresh_session(session_factory, proposal_id,
                                  f"database error while storing the result: {_error_summary(exc)}", now)
        return False


def mark_failed_fresh_session(session_factory, proposal_id, detail: str, now=None,
                              reason_code: str = "PERSISTENCE_FAILED") -> bool:
    """Best effort: turns the row into a terminal FAILED row with no patch content."""
    from api.models import PatchProposal

    try:
        db = session_factory()
        try:
            row = db.query(PatchProposal).filter(PatchProposal.id == proposal_id).first()
            if row is None:
                return False
            row.status, row.reason_code = FAILED, reason_code
            row.reason_detail = _one_line(detail, 300)
            for column in ("unified_diff", "diff_sha256", "patched_git_blob_sha", "proposed_values",
                           "previous_values"):
                setattr(row, column, sa_null())
            row.completed_at = now or _utcnow()
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - the row stays RUNNING and is reported ABANDONED once stale
        return False


def _integrity_problem(row) -> Optional[str]:
    """Why a stored row violates the proposal invariants, or None."""
    if row.status == VALIDATED:
        diff = row.unified_diff
        if not (row.completed_at and isinstance(diff, str) and diff and _sha256_text(diff) == row.diff_sha256
                and row.proposed_values and row.previous_values and row.patched_git_blob_sha):
            return "VALIDATED row without a complete, hash-matching diff"
    elif row.status in (NOT_GENERATED, REJECTED, FAILED, RUNNING):
        if row.unified_diff is not None or row.diff_sha256 is not None or row.proposed_values is not None:
            return f"{row.status} row carries patch content"
    else:
        return f"unknown status {row.status!r}"
    return None


def get_latest_proposal(db, incident_uuid, now=None):
    """Latest proposal row and its effective (status, reason).

    A RUNNING row older than STALE_AFTER_S is reported FAILED/ABANDONED, and a row that breaks
    the stored invariants is reported FAILED/INTEGRITY_MISMATCH. Nothing is written.
    """
    from api.models import PatchProposal

    row = (db.query(PatchProposal).filter(PatchProposal.incident_id == incident_uuid)
           .order_by(PatchProposal.created_at.desc(), PatchProposal.id.desc()).first())
    if row is None:
        return None, None, None
    return (row, *effective_status(row, now))


def effective_status(row, now=None) -> tuple:
    """(status, reason) of a stored row: FAILED/INTEGRITY_MISMATCH, FAILED/ABANDONED or as stored."""
    now = now or _utcnow()
    if _integrity_problem(row):
        return FAILED, "INTEGRITY_MISMATCH"
    if row.status == RUNNING and row.created_at < now - datetime.timedelta(seconds=STALE_AFTER_S):
        return FAILED, "ABANDONED"
    return row.status, row.reason_code


# ─── Orchestration used by the CLI ───

def _system_mode(db) -> str:
    from api.models import SystemConfig

    system = db.query(SystemConfig).filter(SystemConfig.id == "global").first()
    if system is None:
        return "ACTIVE"
    return system.system_mode.value if hasattr(system.system_mode, "value") else str(system.system_mode)


def read_system_mode(session_factory) -> str:
    db = session_factory()
    try:
        return _system_mode(db)
    finally:
        db.close()


def _read_approval(db, incident_uuid, sha: Optional[str]) -> Optional[dict]:
    """The routing row for this incident and report fingerprint, as a plain dict, or None."""
    from api.models import PatchRoutingDecision

    if not sha:
        return None
    row = (db.query(PatchRoutingDecision)
           .filter(PatchRoutingDecision.incident_id == incident_uuid,
                   PatchRoutingDecision.report_sha256 == sha)
           .first())
    if row is None:
        return None
    return {"decision_id": str(row.id), "route": row.route, "decision": row.decision,
            "report_sha256": row.report_sha256,
            "decided_at": row.decided_at.isoformat() + "Z" if row.decided_at else None}


def _read_context(session_factory, incident_uuid):
    """Read-only lookups through the injected session (never the global helpers).

    Returns (snap, cfg, mode, preset, approval). mode is "UNKNOWN" if the kill switch could not
    be read, which is a FAILED/CONFIG_UNAVAILABLE stop; a successfully read DISABLED always wins
    later. approval is the routing row for the snapshot's exact report (or None); if it cannot be
    read it is a ProposalStop, which only matters when the report needs a human approval.
    """
    from api.models import Incident, ProjectConfig

    db = session_factory()
    try:
        incident = db.query(Incident).filter(Incident.id == incident_uuid).first()
        if incident is None:
            return None, {}, "ACTIVE", None, None
        snap = snapshot_incident(incident)
        try:
            mode = _system_mode(db)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            return snap, {}, "UNKNOWN", ProposalStop(FAILED, "CONFIG_UNAVAILABLE",
                                                    f"kill switch could not be read: {_error_summary(exc)}"), None
        try:
            sha = report_sha256(snap["ai_report"]) if isinstance(snap["ai_report"], dict) else None
            approval = _read_approval(db, incident.id, sha)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            approval = ProposalStop(FAILED, "CONFIG_UNAVAILABLE",
                                    f"routing decision could not be read: {_error_summary(exc)}")
        preset, cfg = None, {}
        if snap["project_id"]:
            try:
                project = db.query(ProjectConfig).filter(ProjectConfig.id == incident.project_id).first()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                return snap, {}, mode, ProposalStop(FAILED, "CONFIG_UNAVAILABLE",
                                                   f"project configuration could not be read: {_error_summary(exc)}"), approval
            if project is None:
                preset = ProposalStop(NOT_GENERATED, "CONFIG_INVALID", "incident project row is missing")
            else:
                cfg = {"target_manifest_path": project.target_manifest_path,
                       "target_namespace": project.target_namespace,
                       "github_repo": project.github_repo,
                       "max_resource_scale_factor": project.max_resource_scale_factor,
                       "confidence_threshold": project.confidence_threshold}
        return snap, cfg, mode, preset, approval
    finally:
        db.close()


def _result(exit_code, status, reason_code, reason_detail=None, proposal_id=None, persisted=False, record=None):
    return {"exit_code": exit_code, "status": status, "reason_code": reason_code,
            "reason_detail": reason_detail, "proposal_id": proposal_id, "persisted": persisted, "record": record}


def run_for_incident(session_factory, incident_uuid, repo_root, *, llm=None, env=None,
                     settings: Settings = None, monotonic=time.monotonic, now_fn: Callable = _utcnow) -> dict:
    """Full CLI flow. Returns {exit_code, status, reason_code, reason_detail, proposal_id, persisted, record}.

    Every run that inserted a RUNNING row ends with a terminal row, unless the process is killed.
    On KeyboardInterrupt/SystemExit the row is marked FAILED/INTERRUPTED and the exception re-raised.
    """
    env = os.environ if env is None else env
    settings_error = None
    if settings is None:
        try:
            settings = settings_from_env(env)
        except (TypeError, ValueError) as exc:
            settings_error = ProposalStop(NOT_GENERATED, "CONFIG_INVALID", str(exc))
    try:
        snap, cfg, mode, preset, approval = _read_context(session_factory, incident_uuid)
    except Exception as exc:  # noqa: BLE001 - database unavailable: nothing can be recorded
        return _result(EXIT_NOT_PERSISTED, FAILED, "PERSISTENCE_FAILED", f"database read failed: {_error_summary(exc)}")
    if snap is None:
        return _result(EXIT_USAGE, None, "UNKNOWN_INCIDENT")
    try:
        proposal_id = begin_run(session_factory, incident_uuid, snap["project_id"], now_fn())
    except AlreadyGenerated as existing:
        return _result(EXIT_ALREADY_GENERATED, existing.status, "ALREADY_GENERATED",
                       f"the model already decided for this diagnosis (proposal {existing.proposal_id}: "
                       f"{existing.status}{'/' + existing.reason_code if existing.reason_code else ''}); "
                       "nothing new was generated", existing.proposal_id, True)
    except Exception as exc:  # noqa: BLE001 - never call the LLM if the result cannot be recorded
        return _result(EXIT_NOT_PERSISTED, FAILED, "PERSISTENCE_FAILED", f"database write failed: {_error_summary(exc)}")
    if proposal_id is None:
        return _result(EXIT_BUSY, RUNNING, "BUSY", "another proposal for this incident is still running")

    try:
        rec = propose(snap, cfg, mode, repo_root, llm=llm, settings=settings, env=env,
                      preset_stop=preset or settings_error, monotonic=monotonic,
                      mode_check=lambda: read_system_mode(session_factory), approval=approval)
    except BaseException as exc:  # Ctrl-C or SystemExit during the run: record it, then re-raise
        mark_failed_fresh_session(session_factory, proposal_id, f"run interrupted: {type(exc).__name__}",
                                  now_fn(), reason_code="INTERRUPTED")
        raise
    try:
        persisted = finalize(session_factory, proposal_id, rec, now_fn(), snap=snap)
    except BaseException as exc:
        mark_failed_fresh_session(session_factory, proposal_id, f"run interrupted: {type(exc).__name__}",
                                  now_fn(), reason_code="INTERRUPTED")
        raise
    if not persisted:
        # A result that is not durably stored is only ever reported (or printed) as a failure.
        outcome = f"{rec['status']}/{rec['reason_code']}" if rec["reason_code"] else rec["status"]
        rec.update(status=FAILED, reason_code="PERSISTENCE_FAILED",
                   reason_detail=f"the result ({outcome}) could not be stored; nothing was applied")
        enforce_invariants(rec)
        return _result(EXIT_NOT_PERSISTED, FAILED, "PERSISTENCE_FAILED", rec["reason_detail"], proposal_id, False, rec)
    return _result(EXIT_CODES[rec["status"]], rec["status"], rec["reason_code"], rec.get("reason_detail"),
                   proposal_id, True, rec)


def serialise_proposal(row, effective_status=None, effective_reason=None) -> dict:
    data = {c: getattr(row, c) for c in ("id", "incident_id", "created_at", "completed_at", "proposer_version")}
    data.update({c: getattr(row, c) for c in _COLUMNS})
    data["effective_status"] = effective_status or row.status
    data["effective_reason_code"] = effective_reason if effective_reason is not None else row.reason_code
    if data["effective_status"] != VALIDATED:
        # Only an intact VALIDATED row may show patch content.
        for column in ("unified_diff", "diff_sha256", "patched_git_blob_sha", "proposed_values", "previous_values"):
            data[column] = None
    for column in ("created_at", "completed_at"):
        if data[column] is not None:
            data[column] = data[column].isoformat() + "Z"
    data.update({"applied": False, "requires_human_approval": True, "stage": "PROPOSAL_ONLY"})
    return json.loads(json.dumps(data, default=str))
