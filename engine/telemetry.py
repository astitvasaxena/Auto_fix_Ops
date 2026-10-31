"""
Telemetry collection for the incident context (pipeline Task 1).
=================================================================
Looks up memory and CPU evidence for the alerted workload in Prometheus.
Two sources are tried, in order:

  1. kubernetes_cadvisor - container metrics (container_memory_working_set_bytes,
     container_cpu_usage_seconds_total) when the app runs in a cluster and
     Prometheus scrapes the kubelet/cAdvisor.
  2. process_metrics     - the target app's own process metrics
     (process_resident_memory_bytes, process_cpu_seconds_total) from its scrape
     job. This is what the local docker-compose demo has.

Units are fixed so later stages can compare against manifest limits:
memory in BYTES, CPU in CORES. Each query asks for the peak over the window
(max_over_time), because remediation evidence is about the worst point.

Nothing is invented: when no query returns data, the source is None and the
caller records telemetry as unavailable.
"""

import os
import re
from typing import Callable, Dict, List, Optional

DEFAULT_WORKLOAD = "autofixops-target-app"
DEFAULT_SCRAPE_JOB = "autofixops-target"
WINDOW = "5m"
GROWTH_WINDOW = "3m"   # memory slope at incident time: a leak is still growing, a one-off step is not

# Label values end up inside PromQL strings, so only plain Kubernetes-style
# names are accepted (no quotes, braces or regex characters).
_K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_JOB_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def _plain(value, pattern) -> Optional[str]:
    return value if isinstance(value, str) and pattern.match(value) else None


def telemetry_queries(labels: Dict, namespace_default: str = "autofixops",
                      job: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """PromQL for each source. A source is omitted when its label values are not plain names."""
    labels = labels or {}
    workload = (_plain(labels.get("deployment"), _K8S_NAME) or _plain(labels.get("pod"), _K8S_NAME)
                or _plain(os.getenv("TARGET_WORKLOAD", DEFAULT_WORKLOAD), _K8S_NAME))
    namespace = _plain(labels.get("namespace") or namespace_default, _K8S_NAME)
    job = _plain(job or os.getenv("TARGET_SCRAPE_JOB", DEFAULT_SCRAPE_JOB), _JOB_NAME)

    queries = {}
    if workload and namespace:
        sel = f'namespace="{namespace}", pod=~"{workload}(-.*)?", container!="", container!="POD"'
        queries["kubernetes_cadvisor"] = {
            "memory": f"max_over_time(container_memory_working_set_bytes{{{sel}}}[{WINDOW}])",
            "cpu": f"max_over_time(rate(container_cpu_usage_seconds_total{{{sel}}}[1m])[{WINDOW}:15s])",
            "memory_growth": f"deriv(container_memory_working_set_bytes{{{sel}}}[{GROWTH_WINDOW}])",
        }
    if job:
        sel = f'job="{job}"'
        queries["process_metrics"] = {
            "memory": f"max_over_time(process_resident_memory_bytes{{{sel}}}[{WINDOW}])",
            "cpu": f"max_over_time(rate(process_cpu_seconds_total{{{sel}}}[1m])[{WINDOW}:15s])",
            "memory_growth": f"deriv(process_resident_memory_bytes{{{sel}}}[{GROWTH_WINDOW}])",
        }
    return queries


def collect_telemetry(fetch: Callable[[str], List], labels: Dict,
                      namespace_default: str = "autofixops", job: Optional[str] = None) -> Dict:
    """Runs the queries through `fetch(query) -> Prometheus vector result` and returns
    the first source that has data:

    {"source": "kubernetes_cadvisor" | "process_metrics" | None,
     "metrics": {"memory": [...], "cpu": [...]},   # Prometheus vector results
     "queries": {...}, "units": {"memory": "bytes", "cpu": "cores"}}
    """
    queries = telemetry_queries(labels, namespace_default, job)
    for source, q in queries.items():
        memory, cpu = fetch(q["memory"]) or [], fetch(q["cpu"]) or []
        if memory or cpu:
            return {"source": source, "metrics": {"memory": memory, "cpu": cpu},
                    "memory_growth_mib_per_min": _growth(fetch(q["memory_growth"]) or []),
                    "queries": q, "units": {"memory": "bytes", "cpu": "cores"}}
    return {"source": None, "metrics": {"memory": [], "cpu": []}, "memory_growth_mib_per_min": None,
            "queries": queries, "units": {"memory": "bytes", "cpu": "cores"}}


def _growth(result) -> Optional[float]:
    """Largest memory slope (bytes/s from deriv) as MiB per minute, or None without data."""
    values = []
    for series in result:
        try:
            values.append(float(series["value"][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    values = [v for v in values if v == v]   # drop NaN
    return round(max(values) * 60 / (1024 * 1024), 2) if values else None
