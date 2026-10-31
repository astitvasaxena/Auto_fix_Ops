"""
Kubernetes crash-loop chaos for the local demo (opt-in, allowlisted target only).
================================================================================
The target app reads STARTUP_MEMORY_MB from the optional ConfigMap
`target-app-chaos` and allocates (and writes) that much memory when it starts
(a startup cache that "grew"). Injection sets it to the container's whole memory
limit and deletes the running pod: the cache alone, plus the interpreter's own
heap, exceeds the limit, so every new container is OOMKilled at start and
Kubernetes reports CrashLoopBackOff. (Sizing it against the measured RSS is not
enough: part of RSS is file-backed pages the kernel can evict, so the container
would thrash at the limit instead of being killed.) Because the requirement
lives in the ConfigMap it survives pod replacement; only raising the memory
limit (the remediation) or resetting the chaos value ends it.

The only cluster writes are: patch that one ConfigMap key, and delete the
target's pods. Both are limited to the allowlisted namespace/deployment.
"""

import json
import math

from engine import deployer as dep

CONFIGMAP = "target-app-chaos"
KEY = "STARTUP_MEMORY_MB"
CACHE_SHARE_OF_LIMIT = 1.0            # the start-up cache alone fills the limit -> OOMKilled every time
_MIB = 1024 * 1024


def _configmap_value(kube: dep.Kubectl, namespace: str) -> int:
    r = kube._run(namespace, ["get", "configmap", CONFIGMAP, "--output", "json"])
    if r.returncode != 0:
        raise dep.DeployBlocked("CHAOS_CONFIGMAP_MISSING",
                                f"ConfigMap {CONFIGMAP} not found (apply chaos-configmap.yaml / k8s_demo.py setup)")
    try:
        return int((json.loads(r.stdout).get("data") or {}).get(KEY) or 0)
    except ValueError:
        return 0


def _set_value(kube: dep.Kubectl, namespace: str, mb: int):
    patch = json.dumps({"data": {KEY: str(int(mb))}})
    r = kube._run(namespace, ["patch", "configmap", CONFIGMAP, "--type", "merge", "--patch", patch,
                              "--field-manager", "autofixops-chaos"])
    if r.returncode != 0:
        raise dep.DeployBlocked("CHAOS_FAILED", (r.stderr or "configmap patch failed").strip()[:300])


def _restart_pods(kube: dep.Kubectl, target: dep.Target, live: dict):
    selector = (live["spec"].get("selector") or {}).get("matchLabels") or {}
    label = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    r = kube._run(target.namespace, ["delete", "pods", "--selector", label, "--wait=false"])
    if r.returncode != 0:
        raise dep.DeployBlocked("CHAOS_FAILED", (r.stderr or "pod delete failed").strip()[:300])


def status(cfg: dep.DeployConfig, target: dep.Target, runner=dep.subprocess_runner) -> dict:
    kube = dep.Kubectl(cfg, runner)
    live = kube.get_deployment(target)
    index = dep.container_index(live, target.container)
    return {"startup_memory_mb": _configmap_value(kube, target.namespace),
            "memory_limit": dep.live_limit(live, index, "memory")}


def inject_crash_loop(cfg: dep.DeployConfig, target: dep.Target, baseline_rss_bytes: float,
                      runner=dep.subprocess_runner) -> dict:
    """Sets the start-up cache to the live memory limit and restarts the pods."""
    from engine.patch_generator import parse_memory

    kube = dep.Kubectl(cfg, runner)
    live = kube.get_deployment(target)
    limit = dep.live_limit(live, dep.container_index(live, target.container), "memory")
    mb = max(1, math.ceil(CACHE_SHARE_OF_LIMIT * parse_memory(limit) / _MIB))
    _set_value(kube, target.namespace, mb)
    _restart_pods(kube, target, live)
    return {"fault": "crash_loop", "startup_memory_mb": mb, "memory_limit": limit,
            "baseline_rss_mb": round(baseline_rss_bytes / _MIB, 1),
            "expected": f"start-up needs {mb}Mi + the interpreter > limit {limit}: OOMKilled -> CrashLoopBackOff"}


def reset(cfg: dep.DeployConfig, target: dep.Target, runner=dep.subprocess_runner) -> dict:
    """Removes the start-up requirement and restarts the pods (manual recovery, not remediation)."""
    kube = dep.Kubectl(cfg, runner)
    live = kube.get_deployment(target)
    _set_value(kube, target.namespace, 0)
    _restart_pods(kube, target, live)
    return {"fault": "reset", "startup_memory_mb": 0}
