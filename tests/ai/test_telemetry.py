"""
Telemetry collection for the incident context (engine/telemetry.py).

Run: python tests/ai/test_telemetry.py   (or under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine.telemetry import collect_telemetry, telemetry_queries
from engine.summarizer import ContextSummarizer

LABELS = {"alertname": "HighMemoryUsage", "namespace": "autofixops", "pod": "autofixops-target-app"}


def _vector(value):
    return [{"metric": {}, "value": [1793391047.5, str(value)]}]


def _fetch(answers):
    """answers: substring of the query -> result. Records every query asked."""
    asked = []

    def fetch(query):
        asked.append(query)
        for key, result in answers.items():
            if key in query:
                return result
        return []
    fetch.asked = asked
    return fetch


def test_prefers_kubernetes_container_metrics():
    fetch = _fetch({"container_memory_working_set_bytes": _vector(95e6),
                    "container_cpu_usage_seconds_total": _vector(0.2),
                    "process_resident_memory_bytes": _vector(1)})
    t = collect_telemetry(fetch, LABELS)
    assert t["source"] == "kubernetes_cadvisor"
    assert t["metrics"]["memory"] == _vector(95e6)
    assert not any("process_" in q for q in fetch.asked)


def test_falls_back_to_process_metrics_of_the_scrape_job():
    fetch = _fetch({"process_resident_memory_bytes": _vector(93_000_000),
                    "process_cpu_seconds_total": _vector(0.05)})
    t = collect_telemetry(fetch, LABELS)
    assert t["source"] == "process_metrics"
    assert 'job="autofixops-target"' in t["queries"]["memory"]
    assert t["units"] == {"memory": "bytes", "cpu": "cores"}
    summary = ContextSummarizer.summarize({"metrics": t["metrics"]})
    assert summary.memory_peak == 93_000_000 and summary.cpu_peak == 0.05


def test_no_data_means_unavailable_not_invented():
    t = collect_telemetry(_fetch({}), LABELS)
    assert t["source"] is None
    assert t["metrics"] == {"memory": [], "cpu": []}


def test_peak_not_average_is_requested():
    q = telemetry_queries(LABELS)
    for source in q.values():
        assert source["memory"].startswith("max_over_time(")
        assert source["cpu"].startswith("max_over_time(")


def test_label_values_cannot_inject_promql():
    hostile = {"namespace": 'autofixops"} or vector(1) #', "pod": "x\"}", "deployment": "a.*"}
    q = telemetry_queries(hostile)
    text = " ".join(v for source in q.values() for v in source.values())
    assert "vector(1)" not in text and 'x"' not in text and "a.*" not in text
    # an unsafe namespace drops the Kubernetes source; the fixed scrape job remains
    assert "kubernetes_cadvisor" not in q and "process_metrics" in q


def test_chaos_incident_without_pod_label_uses_default_workload():
    q = telemetry_queries({"alertname": "ChaosInjection:MemoryLeak", "namespace": "autofixops"})
    assert 'pod=~"autofixops-target-app(-.*)?"' in q["kubernetes_cadvisor"]["memory"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
