import logging
from typing import Dict, Any, Optional

from engine.patch_generator import format_cpu, parse_cpu
from engine.patch_safety import FEATURE_FLAG
from engine.telemetry import LEAK_GROWTH_MIB_PER_MIN

logger = logging.getLogger("autofixops")

# CPU usage at or above this share of the CPU limit counts as starved (the HighCPUUsage threshold).
CPU_STARVED_SHARE = 0.9


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _cpu_peak(context: Dict) -> Optional[float]:
    """Largest CPU value (cores) in the collected Prometheus results, or None."""
    values = []
    for series in ((context.get("metrics") or {}).get("cpu") or []):
        try:
            values.append(_number(series["value"][1]))
        except (KeyError, IndexError, TypeError):
            continue
    values = [v for v in values if v is not None]
    return max(values) if values else None

class RuleBasedDiagnosisEngine:
    """
    Phase 3 Baseline: Maps operational evidence directly to remediation actions
    without requiring LLM generation. Deterministic and safe map.
    """
    
    def __init__(self):
        # Bounded taxonomy definition maps directly to Phase 1 alerts.
        # Includes both Kubernetes-native names and Prometheus alert rule names.
        self.rules = {
            # CPU alerts
            "HighCPUUsage": self._handle_cpu_spike,
            # Memory alerts
            "TargetAppMemoryLeak": self._handle_memory_leak,
            "HighMemoryUsage": self._handle_memory_leak,
            # Crash/restart alerts
            "PodCrashLooping": self._handle_crash_loop,
            "TargetAppCrashLoop": self._handle_crash_loop,
            "CrashLoopBackOff": self._handle_crash_loop,
            # Target down
            "TargetAppDown": self._handle_crash_loop,
        }

    def analyze(self, incident_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Entry point for the Engine. Returns a precise Action Output."""
        logger.info(f"Executing Rule-Based Diagnostics for {incident_name}")
        
        handler = self.rules.get(incident_name, self._handle_unknown)
        if incident_name in self.rules:
            verdict = self._from_evidence(context or {})
            if verdict:
                return verdict
        return handler(context)

    def _from_evidence(self, context: Dict) -> Optional[Dict]:
        """Two failures the alert name alone cannot tell apart, decided from code-collected evidence
        (Prometheus telemetry and read-only Kubernetes state). Anything else uses the alert-name rule."""
        k8s = context.get("kubernetes") if isinstance(context.get("kubernetes"), dict) else {}
        s = k8s.get("summary") if k8s.get("available") is True and isinstance(k8s.get("summary"), dict) else None
        if s is None:
            return None
        growth = _number((context.get("telemetry") or {}).get("memory_growth_mib_per_min"))
        flags = s.get("feature_flags") if isinstance(s.get("feature_flags"), dict) else {}
        if growth is not None and growth >= LEAK_GROWTH_MIB_PER_MIN and flags.get(FEATURE_FLAG) == "true":
            return {
                "root_cause_classification": "MEMORY_LEAK_OOM_RISK",
                "confidence": 1.0,
                "reasoning": (f"Memory is still growing ({growth:.1f} MiB/min) while the response cache feature "
                              f"is on; a larger limit only delays the next OOM kill. Turn the feature off."),
                "action": {"type": "DISABLE_FEATURE_FLAG", "target": context.get("target_pod"),
                           "flag": FEATURE_FLAG, "patch_value": "false"},
            }
        peak, limit = _cpu_peak(context), s.get("live_cpu_limit")
        try:
            limit_cores = parse_cpu(limit) / 1000.0 if limit else None
        except (TypeError, ValueError):
            limit_cores = None
        starved = (isinstance(s.get("pods"), int) and s["pods"] >= 1 and s.get("ready_pods") == 0
                   and s.get("restart_count") == 0 and not s.get("last_termination_reason")
                   and isinstance(s.get("readiness_failures"), int) and s["readiness_failures"] >= 1
                   and peak is not None and limit_cores and peak >= CPU_STARVED_SHARE * limit_cores)
        if starved:
            return {
                "root_cause_classification": "CPU_RESOURCE_STARVATION",
                "confidence": 1.0,
                "reasoning": (f"CPU usage reached {peak / limit_cores:.0%} of its {limit} limit and the pod is "
                              f"running but failing its readiness probe, with no restarts or OOM kills."),
                "action": {"type": "INCREASE_CPU_LIMIT", "target": context.get("target_pod"),
                           "patch_value": format_cpu(int(parse_cpu(limit) * 2))},
            }
        return None
        
    def _handle_cpu_spike(self, context: Dict):
        return {
            "root_cause_classification": "CPU_RESOURCE_STARVATION",
            "confidence": 1.0, # Rule based is 100%
            "reasoning": "A known HighCPUUsage alert was caught. Evidence metric exceeds limits.",
            "action": {
                "type": "RESTART_POD",
                "target": context.get("target_pod")
            }
        }
        
    def _handle_memory_leak(self, context: Dict):
        return {
            "root_cause_classification": "MEMORY_LEAK_OOM_RISK",
            "confidence": 1.0,
            "reasoning": "Memory leak detected exceeding limits. Recommend vertical scaling via GitOps.",
            "action": {
                "type": "INCREASE_MEMORY_LIMIT",
                "target": context.get("target_pod"),
                "patch_value": "200Mi" # Hardcoded safe baseline
            }
        }
        
    def _handle_crash_loop(self, context: Dict):
        return {
            "root_cause_classification": "CRASH_LOOP_BACKOFF",
            "confidence": 1.0,
            "reasoning": "Liveness failures triggered backoff.",
            "action": {
                "type": "ROLLBACK_DEPLOYMENT",
                "target": context.get("target_pod")
            }
        }
        
    def _handle_unknown(self, context: Dict):
        return {
            "root_cause_classification": "UNKNOWN_ANOMALY",
            "confidence": 0.0,
            "reasoning": "No deterministic rule exists for this incident type.",
            "action": {
                "type": "ESCALATE",
                "target": "human"
            }
        }
