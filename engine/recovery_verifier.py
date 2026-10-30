"""
Recovery verification for Stage 4.
==================================
After a remediation is rolled out, one *round* of checks is run every
VERIFY_INTERVAL_SECONDS. The incident is RESOLVED only when every check has
passed continuously for VERIFY_STABILIZATION_SECONDS. If that has not happened
within VERIFY_TIMEOUT_SECONDS, verification fails and the change is rolled back.

Checks in a round (all must pass):
  deployment_ready  observed generation is current and all replicas are updated,
                    ready and available
  limit_applied     the live limit equals the approved new value
  pods_healthy      every pod is Running and Ready, none is waiting in
                    CrashLoopBackOff, and no restart happened since the first round
  app_health        HTTP GET VERIFY_HEALTH_URL returns 200
  alert_cleared     Prometheus has no firing ALERTS for the incident's alert name
  resource_headroom current usage is below 80% of the new limit
A check that cannot be evaluated (e.g. Prometheus unreachable) counts as failed.
"""

import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, List

from engine.deployer import DeployBlocked, Kubectl, Target, container_index, live_limit
from engine.patch_generator import parse_cpu, parse_memory
from engine.telemetry import DEFAULT_SCRAPE_JOB

HEADROOM_MAX_UTILISATION = 0.80
_ALERT_NAME = re.compile(r"^[A-Za-z0-9_:]{1,100}$")


@dataclass(frozen=True)
class VerifyConfig:
    health_url: str
    interval_s: int
    stabilization_s: int
    timeout_s: int


def verify_config(env=None) -> VerifyConfig:
    env = os.environ if env is None else env

    def number(name, default, low, high):
        try:
            value = int(env.get(name, default))
        except (TypeError, ValueError):
            raise DeployBlocked("VERIFY_CONFIG_INVALID", f"{name} is not an integer")
        if not low <= value <= high:
            raise DeployBlocked("VERIFY_CONFIG_INVALID", f"{name} must be between {low} and {high}")
        return value

    url = (env.get("VERIFY_HEALTH_URL") or "").strip()
    if not re.match(r"^https?://[A-Za-z0-9.\-:\[\]]+(/[A-Za-z0-9._~/\-]*)?$", url):
        raise DeployBlocked("VERIFY_CONFIG_INVALID", "VERIFY_HEALTH_URL must be an http(s) URL of the target's health endpoint")
    cfg = VerifyConfig(health_url=url, interval_s=number("VERIFY_INTERVAL_SECONDS", 10, 2, 300),
                       stabilization_s=number("VERIFY_STABILIZATION_SECONDS", 60, 0, 3600),
                       timeout_s=number("VERIFY_TIMEOUT_SECONDS", 300, 30, 7200))
    if cfg.timeout_s <= cfg.stabilization_s:
        raise DeployBlocked("VERIFY_CONFIG_INVALID", "VERIFY_TIMEOUT_SECONDS must exceed VERIFY_STABILIZATION_SECONDS")
    return cfg


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail[:300]}


def _usage_queries(resource: str, t: Target) -> List[str]:
    job = os.getenv("TARGET_SCRAPE_JOB", DEFAULT_SCRAPE_JOB)
    sel = f'namespace="{t.namespace}", pod=~"{t.name}(-.*)?", container="{t.container}"'
    if resource == "memory":
        return [f"max(container_memory_working_set_bytes{{{sel}}})",
                f'max(process_resident_memory_bytes{{job="{job}"}})']
    return [f"max(rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))",
            f'max(rate(process_cpu_seconds_total{{job="{job}"}}[1m]))']


def run_round(kube: Kubectl, t: Target, cfg: VerifyConfig, alert_name: str,
              http_get: Callable[[str], int], prom_query: Callable[[str], list],
              baseline_restarts: Dict[str, int] = None) -> dict:
    """One round of checks. Returns {"checks": [...], "ok": bool, "restarts": {pod: count}}."""
    checks, restarts = [], {}
    try:
        live = kube.get_deployment(t)
        meta, spec, status = live.get("metadata") or {}, live.get("spec") or {}, live.get("status") or {}
        want = spec.get("replicas", 1)
        ready = (status.get("observedGeneration", 0) >= meta.get("generation", 0)
                 and status.get("updatedReplicas", 0) == want and status.get("readyReplicas", 0) == want
                 and status.get("availableReplicas", 0) == want)
        checks.append(_check("deployment_ready", ready,
                             f"replicas {want}, updated {status.get('updatedReplicas', 0)}, "
                             f"ready {status.get('readyReplicas', 0)}, available {status.get('availableReplicas', 0)}"))
        value = live_limit(live, container_index(live, t.container), t.resource)
        checks.append(_check("limit_applied", value == t.new_value, f"live {t.resource} limit {value}"))
        pods = kube.get_pods(t, (spec.get("selector") or {}).get("matchLabels") or {})
        problems = []
        for pod in pods:
            name = pod["metadata"]["name"]
            if pod.get("metadata", {}).get("deletionTimestamp"):
                continue   # an old pod still terminating after the rollout
            conditions = {c.get("type"): c.get("status") for c in (pod.get("status") or {}).get("conditions") or []}
            if (pod.get("status") or {}).get("phase") != "Running" or conditions.get("Ready") != "True":
                problems.append(f"{name} not running/ready")
            for cs in (pod.get("status") or {}).get("containerStatuses") or []:
                restarts[name] = restarts.get(name, 0) + int(cs.get("restartCount", 0))
                reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
                if reason:
                    problems.append(f"{name} waiting: {reason}")
            if baseline_restarts and name in baseline_restarts and restarts.get(name, 0) > baseline_restarts[name]:
                problems.append(f"{name} restarted during verification")
        if not pods:
            problems.append("no pods found")
        checks.append(_check("pods_healthy", not problems, "; ".join(problems) or f"{len(pods)} pod(s) running and ready"))
    except DeployBlocked as exc:
        checks.append(_check("deployment_ready", False, f"{exc.code}: {exc.detail}"))

    try:
        code = http_get(cfg.health_url)
        checks.append(_check("app_health", code == 200, f"GET {cfg.health_url} -> {code}"))
    except Exception as exc:   # connection refused, timeout
        checks.append(_check("app_health", False, f"GET {cfg.health_url} failed: {exc.__class__.__name__}"))

    try:
        if not _ALERT_NAME.match(alert_name or ""):
            checks.append(_check("alert_cleared", False, "incident alert name is not a plain identifier"))
        else:
            firing = prom_query(f'ALERTS{{alertname="{alert_name}", alertstate="firing"}}')
            checks.append(_check("alert_cleared", not firing,
                                 f"{alert_name} {'still firing' if firing else 'not firing'}"))
        usage = None
        for q in _usage_queries(t.resource, t):
            result = prom_query(q)
            if result:
                usage = float(result[0]["value"][1])
                break
        if usage is None:
            checks.append(_check("resource_headroom", False, "no current usage data in Prometheus"))
        else:
            limit = parse_memory(t.new_value) if t.resource == "memory" else parse_cpu(t.new_value) / 1000.0
            share = usage / limit if limit else float("inf")
            checks.append(_check("resource_headroom", share < HEADROOM_MAX_UTILISATION,
                                 f"usage is {share:.0%} of the new {t.resource} limit {t.new_value}"))
    except Exception as exc:
        checks.append(_check("alert_cleared", False, f"Prometheus query failed: {exc.__class__.__name__}"))
    return {"checks": checks, "ok": all(c["ok"] for c in checks), "restarts": restarts}
