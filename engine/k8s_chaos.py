"""
Kubernetes chaos for the local demo (opt-in, allowlisted target only).
======================================================================
Each fault is a value in the optional ConfigMap `target-app-chaos` that the
target app reads when it starts (kubernetes_integration/target_app/app.py).
Injection writes the value plus CHAOS_EXPIRES_AT and deletes the running pod,
so the fault is real, survives pod replacement, and ends by itself at the
expiry time (CHAOS_TTL_MINUTES, default 30) or when it is reset.

  crash_loop   STARTUP_MEMORY_MB = the whole memory limit: the start-up cache
               alone (plus the interpreter) exceeds it, so every start is
               OOMKilled -> CrashLoopBackOff. (Sizing it against the measured
               RSS is not enough: part of RSS is evictable file-backed pages,
               and the container would thrash instead of being killed.)
               Fixed by a larger memory limit.
  memory_leak  LEAK_MB_PER_MIN: the app's response cache stops evicting and
               grows until the container is OOMKilled, again after every
               restart. A larger limit only delays the next kill; the fix is to
               turn the cache off with the version-controlled feature flag
               RESPONSE_CACHE_ENABLED in deployment.yaml (DISABLE_FEATURE_FLAG).
               Needs that flag set to "true" on the live Deployment.
  cpu_spike    CPU_DEMAND_MILLICORES = 130% of the CPU limit: background work
               that one thread cannot finish under the limit. Its backlog grows,
               the readiness probe (/ready) fails and the pod leaves service:
               Running, 0/1 Ready, no restarts. Fixed by a larger CPU limit (2x
               leaves the work at 65% of it). Needs the /ready readiness probe and
               a demand of at most 1000m (one thread), so a CPU limit <= 769m.

The only cluster writes are: patch keys of that one ConfigMap, and delete the
target's pods. Both are limited to the allowlisted namespace/deployment.
"""

import json
import math
import os
import time

from engine import deployer as dep
from engine.patch_safety import FEATURE_FLAG

CONFIGMAP = "target-app-chaos"
KEYS = ("STARTUP_MEMORY_MB", "LEAK_MB_PER_MIN", "CPU_DEMAND_MILLICORES")
KEY = "STARTUP_MEMORY_MB"              # kept for callers of the crash-loop-only version
FAULTS = ("crash_loop", "memory_leak", "cpu_spike")
CACHE_SHARE_OF_LIMIT = 1.0            # the start-up cache alone fills the limit -> OOMKilled every time
LEAK_MB_PER_MIN = 40                  # +80 MB in 2 minutes: the TargetAppMemoryLeak rule fires
CPU_DEMAND_SHARE_OF_LIMIT = 1.3       # 650m of work for a 500m limit; a 2x limit leaves it at 65%
MAX_CPU_DEMAND_MILLICORES = 1000      # one thread
READY_PATH = "/ready"
_MIB = 1024 * 1024


def ttl_minutes(env=None) -> int:
    env = os.environ if env is None else env
    try:
        return max(1, min(120, int(env.get("CHAOS_TTL_MINUTES", 30))))
    except (TypeError, ValueError):
        return 30


def _configmap(kube: dep.Kubectl, namespace: str) -> dict:
    r = kube._run(namespace, ["get", "configmap", CONFIGMAP, "--output", "json"])
    if r.returncode != 0:
        raise dep.DeployBlocked("CHAOS_CONFIGMAP_MISSING",
                                f"ConfigMap {CONFIGMAP} not found (apply chaos-configmap.yaml / k8s_demo.py setup)")
    try:
        return json.loads(r.stdout).get("data") or {}
    except ValueError:
        return {}


def _number(data: dict, key: str) -> float:
    try:
        return float(data.get(key) or 0)
    except ValueError:
        return 0.0


def _write(kube: dep.Kubectl, namespace: str, values: dict):
    """Sets every chaos key (unnamed ones to 0) in one merge patch."""
    data = {k: str(int(values.get(k, 0))) for k in KEYS}
    data["CHAOS_EXPIRES_AT"] = str(int(values.get("CHAOS_EXPIRES_AT", 0)))
    r = kube._run(namespace, ["patch", "configmap", CONFIGMAP, "--type", "merge", "--patch",
                              json.dumps({"data": data}), "--field-manager", "autofixops-chaos"])
    if r.returncode != 0:
        raise dep.DeployBlocked("CHAOS_FAILED", (r.stderr or "configmap patch failed").strip()[:300])


def _restart_pods(kube: dep.Kubectl, target: dep.Target, live: dict):
    selector = (live["spec"].get("selector") or {}).get("matchLabels") or {}
    label = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    r = kube._run(target.namespace, ["delete", "pods", "--selector", label, "--wait=false"])
    if r.returncode != 0:
        raise dep.DeployBlocked("CHAOS_FAILED", (r.stderr or "pod delete failed").strip()[:300])


def _readiness_path(live: dict, index: int):
    probe = live["spec"]["template"]["spec"]["containers"][index].get("readinessProbe") or {}
    return (probe.get("httpGet") or {}).get("path")


def status(cfg: dep.DeployConfig, target: dep.Target, runner=dep.subprocess_runner, now=None) -> dict:
    now = time.time() if now is None else now
    kube = dep.Kubectl(cfg, runner)
    live = kube.get_deployment(target)
    index = dep.container_index(live, target.container)
    data = _configmap(kube, target.namespace)
    expires = _number(data, "CHAOS_EXPIRES_AT")
    active = expires > now
    faults = {"crash_loop": _number(data, "STARTUP_MEMORY_MB"), "memory_leak": _number(data, "LEAK_MB_PER_MIN"),
              "cpu_spike": _number(data, "CPU_DEMAND_MILLICORES")}
    return {"active": active and any(faults.values()),
            "active_faults": [f for f, v in faults.items() if v and active],
            "expires_at": expires or None, "seconds_left": max(0, int(expires - now)) if active else 0,
            "startup_memory_mb": int(faults["crash_loop"]), "leak_mb_per_min": int(faults["memory_leak"]),
            "cpu_demand_millicores": int(faults["cpu_spike"]),
            "memory_limit": dep.live_limit(live, index, "memory"), "cpu_limit": dep.live_limit(live, index, "cpu"),
            "feature_flags": dep.live_flags(live, index), "readiness_path": _readiness_path(live, index)}


def inject(cfg: dep.DeployConfig, target: dep.Target, fault: str, runner=dep.subprocess_runner,
           now=None, env=None) -> dict:
    """Writes one fault (clearing the others), sets its expiry and restarts the target's pods."""
    from engine.patch_generator import parse_cpu, parse_memory

    if fault not in FAULTS:
        raise dep.DeployBlocked("CHAOS_INVALID", f"unknown fault {fault!r}")
    now = time.time() if now is None else now
    kube = dep.Kubectl(cfg, runner)
    live = kube.get_deployment(target)
    index = dep.container_index(live, target.container)
    memory, cpu = dep.live_limit(live, index, "memory"), dep.live_limit(live, index, "cpu")
    minutes = ttl_minutes(env)
    values = {"CHAOS_EXPIRES_AT": now + minutes * 60}
    if fault == "crash_loop":
        values["STARTUP_MEMORY_MB"] = max(1, math.ceil(CACHE_SHARE_OF_LIMIT * parse_memory(memory) / _MIB))
        expected = (f"start-up needs {values['STARTUP_MEMORY_MB']}Mi + the interpreter > memory limit {memory}: "
                    f"OOMKilled at every start -> CrashLoopBackOff")
    elif fault == "memory_leak":
        flag = dep.live_flags(live, index).get(FEATURE_FLAG)
        if flag != "true":
            raise dep.DeployBlocked("CHAOS_INVALID", (
                f"the leak is in the response cache, which is off ({FEATURE_FLAG}={flag!r}); re-enable it through "
                f"a reviewed change (revert the remediation PR)" if flag == "false" else
                f"the live Deployment does not set {FEATURE_FLAG}; apply the current deployment.yaml first"))
        values["LEAK_MB_PER_MIN"] = LEAK_MB_PER_MIN
        expected = (f"the response cache grows {LEAK_MB_PER_MIN} MB/min until the {memory} limit -> OOMKilled, "
                    f"again after every restart")
    else:
        if not cpu:
            raise dep.DeployBlocked("CHAOS_INVALID", "the target has no CPU limit to saturate")
        if _readiness_path(live, index) != READY_PATH:
            raise dep.DeployBlocked("CHAOS_INVALID", f"the live readiness probe is not {READY_PATH}; apply the "
                                                     f"current deployment.yaml (and target image) first")
        demand = math.ceil(CPU_DEMAND_SHARE_OF_LIMIT * parse_cpu(cpu))
        if demand > MAX_CPU_DEMAND_MILLICORES:
            raise dep.DeployBlocked("CHAOS_INVALID", (
                f"CPU limit {cpu} is too high: the work runs in one thread (at most "
                f"{MAX_CPU_DEMAND_MILLICORES}m), so it cannot exceed it; revert an earlier CPU remediation first"))
        values["CPU_DEMAND_MILLICORES"] = demand
        expected = (f"background work needs {demand}m > CPU limit {cpu}: its backlog grows, the readiness "
                    f"probe fails and the pod leaves service (Running, 0/1 Ready)")
    _write(kube, target.namespace, values)
    _restart_pods(kube, target, live)
    return {"fault": fault, "expected": expected, "memory_limit": memory, "cpu_limit": cpu,
            "expires_in_minutes": minutes, **{k.lower(): int(v) for k, v in values.items() if k in KEYS}}


def inject_crash_loop(cfg: dep.DeployConfig, target: dep.Target, baseline_rss_bytes: float = 0.0,
                      runner=dep.subprocess_runner) -> dict:
    """Crash-loop fault (kept for existing callers); baseline_rss_bytes is reported only."""
    result = inject(cfg, target, "crash_loop", runner=runner)
    result["baseline_rss_mb"] = round(baseline_rss_bytes / _MIB, 1)
    return result


def reset(cfg: dep.DeployConfig, target: dep.Target, runner=dep.subprocess_runner) -> dict:
    """Clears every fault and restarts the pods (manual recovery, not remediation)."""
    kube = dep.Kubectl(cfg, runner)
    live = kube.get_deployment(target)
    _write(kube, target.namespace, {})
    _restart_pods(kube, target, live)
    return {"fault": "reset", "startup_memory_mb": 0, "leak_mb_per_min": 0, "cpu_demand_millicores": 0}
