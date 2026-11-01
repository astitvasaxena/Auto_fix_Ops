"""
Chaos scenarios drill: each Kubernetes fault, its evidence, its own remediation, verified recovery.
===================================================================================================
- CPU Spike: background work above the CPU limit -> backlog -> /ready fails -> pod Running, 0/1 Ready.
  Evidence: readiness-probe failures, no restarts/OOM, CPU measured at the limit. Fix: CPU limit (2x).
- Memory Leak: the response cache grows while RESPONSE_CACHE_ENABLED is "true". Evidence: memory still
  growing + the live flag. Fix: DISABLE_FEATURE_FLAG ("true" -> "false" in deployment.yaml), never a
  larger memory limit. Verification also requires memory to stop growing after the deployment.
- Crash Loop is covered by test_crash_loop.py and must keep working unchanged.
Every flow goes through the real proposer, approval, PR, CI gate, merge check, kubectl JSON patch and
verification, with fakes for GitHub, kubectl and Prometheus. No network, cluster or LLM.

Run: python tests/remediation/test_chaos_scenarios.py   (or under pytest)
"""
import os
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "ai"))
sys.path.insert(0, str(HERE))

import test_crash_loop as cl
import test_patch_proposals as base
import test_remediation_workflow as wft
from api.models import Incident, IncidentStatus
from engine import deployer as dep
from engine import k8s_chaos, k8s_evidence, recovery_verifier as rv
from engine import patch_proposer as pp
from engine import remediation_workflow as wf
from engine.ai_diagnostics import AIIncidentReport, build_evidence_query
from engine.baseline import RuleBasedDiagnosisEngine
from engine.patch_diff import PatchDiffError, check_diff_policy, parse_unified_diff_strict, make_unified_diff
from engine.patch_generator import HARD_CAPS, PatchBoundsError, PatchGenerator, format_cpu, parse_cpu

MIB = 1024 ** 2
FLAG_LINE = '          value: "true"'
FLAG_MANIFEST = base.MANIFEST_LF.replace(
    "        - containerPort: 8000\n",
    "        - containerPort: 8000\n        env:\n        - name: RESPONSE_CACHE_ENABLED\n" + FLAG_LINE + "\n", 1)
REAL_MANIFEST = base.REAL_MANIFEST.read_bytes().decode("utf-8").replace("\r\n", "\n")
POD = "autofixops-target-app-7d9-x1"
READINESS_EVENT = {"reason": "Unhealthy", "message": "Readiness probe failed: HTTP probe failed with statuscode: 503",
                   "count": 6, "involvedObject": {"name": POD}, "lastTimestamp": "2026-10-31T12:01:00Z"}


def _load_app():
    """The target app, loaded once per process (it registers Prometheus collectors on import)."""
    import importlib.util
    app = sys.modules.get("target_app_under_test")
    if app is None:
        spec = importlib.util.spec_from_file_location(
            "target_app_under_test", str(REPO_ROOT / "kubernetes_integration" / "target_app" / "app.py"))
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        sys.modules["target_app_under_test"] = app
    return app


def _real_container():
    """The real manifest's container as it is now. A merged remediation PR changes its CPU limit or its
    feature flag, so tests that run against it take those values from it (as base.real_manifest_report does)."""
    import yaml

    return yaml.safe_load(REAL_MANIFEST)["spec"]["template"]["spec"]["containers"][0]


def _evidence(**cluster):
    return k8s_evidence.collect(cl.LABELS, env=cl.ENV, runner=cl.FakeCluster(**cluster))


def _starved_evidence(readiness_events=(READINESS_EVENT,), **cluster):
    """A pod that is running, not ready, never restarted (the CPU Spike outage)."""
    kw = dict(reason=None, restarts=0, waiting=None, extra_events=readiness_events)
    kw.update(cluster)
    return _evidence(**kw)


def _leak_evidence(**cluster):
    kw = dict(reason=None, restarts=0, waiting=None)
    kw.update(cluster)
    return _evidence(**kw)


def _raw(evidence, growth=0.0):
    return {"labels": cl.LABELS, "raw_context": {
        "telemetry": {"cpu_available": True, "memory_available": True, "memory_growth_mib_per_min": growth},
        "kubernetes": evidence}}


def _down_cpu_report(cpu_peak=0.49, **overrides):
    report = base._cpu_report(incident_type="TARGET_APP_DOWN", incident_summary="The target is out of service.",
                              rule_engine={"root_cause_classification": "CPU_RESOURCE_STARVATION",
                                           "action": {"type": "INCREASE_CPU_LIMIT", "patch_value": "1"}})
    report["incident_context"]["alert"]["name"] = "TargetAppDown"
    report["incident_context"]["telemetry"]["summary"]["cpu_peak"] = cpu_peak
    report.update(overrides)
    return report


def _leak_report(**overrides):
    report = base._report(
        incident_summary="Memory keeps growing.",
        proposed_action={"type": "DISABLE_FEATURE_FLAG", "target": "target-app", "rationale": "leak in the cache"},
        rule_engine={"root_cause_classification": "MEMORY_LEAK_OOM_RISK",
                     "action": {"type": "DISABLE_FEATURE_FLAG", "flag": "RESPONSE_CACHE_ENABLED", "patch_value": "false"}})
    report["incident_context"]["alert"]["name"] = "TargetAppMemoryLeak"
    report.update(overrides)
    return report


def _llm():
    return base.MenuStubLLM(rationale="Memory keeps growing while the response cache is on; turning it off stops it.")


# ─── Target app: genuine faults ───

def test_cpu_work_backlog_grows_only_when_cpu_is_short_and_fails_readiness():
    from fastapi import Response

    app = _load_app()
    samples = {}

    def run(millicores, seconds, cpu_share):
        # cpu_share < 1 models a CPU limit (as a cgroup quota would): only that share of the thread's
        # real CPU time counts as work done, so the work needs proportionally more CPU than it gets.
        stop = time.time() + seconds
        clock = (lambda: time.thread_time() * cpu_share) if cpu_share < 1 else time.thread_time
        worker = threading.Thread(target=app._process_work, args=(millicores, stop),
                                  kwargs={"cpu_clock": clock}, daemon=True)
        worker.start()
        time.sleep(seconds - 0.25)
        backlog = app.work["backlog_s"]
        worker.join(2)
        return backlog

    samples["enough_cpu"] = run(300, 1.2, cpu_share=1)
    samples["starved"] = run(900, 1.6, cpu_share=0.5)    # 900m of work, half a core: backlog ~0.4s/s
    assert samples["enough_cpu"] < 0.15, samples
    assert samples["starved"] > 0.25, samples
    assert app.work["backlog_s"] == 0.0                                  # stops (and clears) at its expiry
    app.work["backlog_s"] = app.READY_MAX_BACKLOG_S + 5
    response = Response()
    assert app.ready(response)["status"] == "overloaded" and response.status_code == 503
    assert app.health() == {"status": "ok"}                             # liveness unaffected: no restarts
    app.work["backlog_s"] = 0.2
    response = Response()
    assert app.ready(response)["status"] == "ready" and response.status_code == 200
    print(f"✅ PASS: work backlog {samples['enough_cpu']:.2f}s with CPU to spare, {samples['starved']:.2f}s when "
          f"starved; /ready 503 above {app.READY_MAX_BACKLOG_S:g}s, /health stays 200")


def test_response_cache_flag_controls_the_leak():
    from unittest.mock import patch

    app = _load_app()
    env = {"LEAK_MB_PER_MIN": "40", "CHAOS_EXPIRES_AT": str(time.time() + 60)}
    with patch.dict(os.environ, {**env, "RESPONSE_CACHE_ENABLED": "true"}):
        assert app.chaos_settings()["leak_mb_per_min"] == 40
    with patch.dict(os.environ, {**env, "RESPONSE_CACHE_ENABLED": "false"}):
        s = app.chaos_settings()
        assert s["leak_mb_per_min"] == 0 and s["response_cache_enabled"] is False and s["active"]
    print("✅ PASS: the leak runs only while RESPONSE_CACHE_ENABLED is true; the fault itself stays set")


def test_manifest_has_the_flag_and_a_separate_readiness_probe():
    c = _real_container()
    # "true" until a merged leak remediation turns it off; either way a quoted string the proposer can patch.
    flags = [e.get("value") for e in c["env"] if e.get("name") == "RESPONSE_CACHE_ENABLED"]
    assert flags in (["true"], ["false"]), flags
    assert '- name: RESPONSE_CACHE_ENABLED\n          value: "' in REAL_MANIFEST
    assert c["readinessProbe"]["httpGet"]["path"] == "/ready" and c["livenessProbe"]["httpGet"]["path"] == "/health"
    print(f"✅ PASS: deployment.yaml: feature flag RESPONSE_CACHE_ENABLED={flags[0]!r}, readiness /ready, liveness /health")


# ─── Chaos injection guards ───

def test_chaos_faults_refuse_a_cluster_that_cannot_show_them():
    cfg = dep.read_config(cl.ENV)
    target = dep.Target("autofixops", "autofixops-target-app", "target-app", "memory", "0", "0")

    def refused(fault, **cluster):
        c = cl.FakeCluster(restarts=0, **cluster)
        try:
            k8s_chaos.inject(cfg, target, fault, runner=c)
        except dep.DeployBlocked as exc:
            assert exc.code == "CHAOS_INVALID" and all(a[5] == "get" for a in c.argvs), c.argvs   # nothing written
            return exc.detail
        raise AssertionError(f"{fault} {cluster} was not refused")

    assert "revert the remediation PR" in refused("memory_leak", flag="false")
    assert "apply the current deployment.yaml" in refused("memory_leak", flag=None)
    assert "readiness probe is not /ready" in refused("cpu_spike", readiness="/health")
    assert "too high" in refused("cpu_spike", cpu="1")                  # 1300m of work cannot run in one thread
    c = cl.FakeCluster(restarts=0)
    assert k8s_chaos.inject(cfg, target, "cpu_spike", runner=c)["cpu_demand_millicores"] == 650
    st = k8s_chaos.status(cfg, target, runner=c)
    assert st["feature_flags"] == {"RESPONSE_CACHE_ENABLED": "true"} and st["readiness_path"] == "/ready"
    print("✅ PASS: leak needs the cache flag on, CPU spike needs /ready and a limit one thread can exceed")


# ─── Evidence, rule engine, analyst query ───

def test_evidence_reports_readiness_failures_and_feature_flags():
    s = _starved_evidence(readiness_events=(READINESS_EVENT, {
        "reason": "Unhealthy", "message": "Liveness probe failed: timeout", "count": 2,
        "involvedObject": {"name": POD}, "lastTimestamp": "2026-10-31T12:01:01Z"}))["summary"]
    assert (s["ready_pods"], s["restart_count"], s["readiness_failures"], s["liveness_failures"]) == (0, 0, 6, 2)
    assert s["feature_flags"] == {"RESPONSE_CACHE_ENABLED": "true"} and s["live_cpu_limit"] == "500m"
    print("✅ PASS: evidence: 0/1 ready, 6 readiness / 2 liveness probe failures, live feature flags")


def test_rule_engine_tells_the_three_faults_apart_from_evidence():
    engine = RuleBasedDiagnosisEngine()
    cpu = [{"metric": {}, "value": [0, "0.49"]}]
    starved = engine.analyze("TargetAppDown", {"kubernetes": _starved_evidence(), "metrics": {"cpu": cpu}})
    assert (starved["root_cause_classification"], starved["action"]["type"], starved["action"]["patch_value"]) == (
        "CPU_RESOURCE_STARVATION", "INCREASE_CPU_LIMIT", "1")
    leak = engine.analyze("TargetAppMemoryLeak", {"kubernetes": _leak_evidence(),
                                                  "telemetry": {"memory_growth_mib_per_min": 30.0}})
    assert leak["action"] == {"type": "DISABLE_FEATURE_FLAG", "target": None, "flag": "RESPONSE_CACHE_ENABLED",
                              "patch_value": "false"}
    crash = engine.analyze("TargetAppDown", {"kubernetes": _evidence(), "metrics": {"cpu": []}})
    assert crash["root_cause_classification"] == "CRASH_LOOP_BACKOFF"            # unchanged for the crash loop
    flag_off = engine.analyze("TargetAppMemoryLeak", {"kubernetes": _leak_evidence(flag="false"),
                                                      "telemetry": {"memory_growth_mib_per_min": 30.0}})
    assert flag_off["action"]["type"] == "INCREASE_MEMORY_LIMIT"                  # alert-name rule; Stage 2 refuses it
    low = engine.analyze("TargetAppDown", {"kubernetes": _starved_evidence(), "metrics": {"cpu": [
        {"metric": {}, "value": [0, "0.2"]}]}})
    assert low["root_cause_classification"] == "CRASH_LOOP_BACKOFF"               # not near the CPU limit
    assert engine.analyze("SomethingElse", {"kubernetes": _starved_evidence(),
                                            "metrics": {"cpu": cpu}})["action"]["type"] == "ESCALATE"
    print("✅ PASS: rule engine: starved+not ready -> CPU limit, growing+flag on -> flag off, OOM crash loop unchanged")


def test_analyst_vocabulary_and_evidence_query():
    report = {k: v for k, v in _leak_report().items() if k in AIIncidentReport.model_fields}
    assert AIIncidentReport.model_validate(report).proposed_action.type == "DISABLE_FEATURE_FLAG"

    def ctx(evidence, growth=None, cpu_peak=0.1):
        return {"telemetry": {"summary": {"cpu_peak": cpu_peak}, "memory_growth_mib_per_min": growth},
                "kubernetes": {"summary": evidence["summary"]}}

    assert build_evidence_query(ctx(_starved_evidence(), cpu_peak=0.49)) == (
        "CPU usage at its CPU limit, CPU throttling. Pod running but not ready, readiness probe failing under load")
    assert build_evidence_query(ctx(_leak_evidence(), growth=30.0)) == "memory growing, memory leak suspected"
    assert build_evidence_query(ctx(_evidence())) is None                       # crash loop: main query only
    print("✅ PASS: DISABLE_FEATURE_FLAG is in the analyst vocabulary; evidence-only runbook query for CPU/leak")


# ─── Proposer: CPU Spike outage ───

def test_cpu_starved_down_target_gets_the_bounded_cpu_maximum():
    llm = base.MenuStubLLM()
    result, _, SF, _ = base.run_case(report=_down_cpu_report(), raw_payload=_raw(_starved_evidence()), llm=llm)
    assert result["status"] == pp.VALIDATED, (result["reason_code"], result.get("reason_detail"))
    row = base._rows(SF)[0]
    assert row.candidates["values"] == ["1"] and row.proposed_values["new_value"] == "1"
    facts = {f["id"]: f["text"] for f in row.evidence["facts_offered"]}
    assert "not ready" in facts["K1"] and "6 readiness probe failures" in facts["K1"] and "98%" in facts["F4"]
    assert "MENU for the cpu limit: 1" in llm.prompts[0]
    llm = base.MenuStubLLM()                        # 92%: outside the saturation band, still only the maximum
    result, _, SF3, _ = base.run_case(report=_down_cpu_report(cpu_peak=0.46), raw_payload=_raw(_starved_evidence()),
                                      llm=llm)
    assert result["status"] == pp.VALIDATED and base._rows(SF3)[0].candidates["values"] == ["1"], result
    # The real manifest at its current CPU limit: the pod ran with it and peaked at 98% of it.
    real_cpu = _real_container()["resources"]["limits"]["cpu"]
    real_mc = parse_cpu(real_cpu)
    expected = format_cpu(min(2 * real_mc, parse_cpu(HARD_CAPS["cpu"])))
    rule = {"root_cause_classification": "CPU_RESOURCE_STARVATION",
            "action": {"type": "INCREASE_CPU_LIMIT", "patch_value": expected}}
    real, _, SF2, _ = base.run_case(report=_down_cpu_report(cpu_peak=0.98 * real_mc / 1000, rule_engine=rule),
                                    raw_payload=_raw(_starved_evidence(cpu=real_cpu)),
                                    llm=base.MenuStubLLM(), manifest=REAL_MANIFEST)
    assert real["status"] == pp.VALIDATED and base._rows(SF2)[0].proposed_values["new_value"] == expected, (real_cpu, real)
    print(f"✅ PASS: TARGET_APP_DOWN + running-not-ready + CPU at 98% of 500m -> VALIDATED 1 core "
          f"(real manifest: {real_cpu} -> {expected})")


def test_cpu_outage_needs_starvation_evidence():
    cases = [
        ("no readiness failures", _down_cpu_report(), _starved_evidence(readiness_events=()), "INCONSISTENT_DIAGNOSIS"),
        ("restarting (OOMKilled)", _down_cpu_report(), _starved_evidence(reason="OOMKilled", restarts=3),
         "INCONSISTENT_DIAGNOSIS"),
        ("restarted (no reason)", _down_cpu_report(), _starved_evidence(restarts=2), "INCONSISTENT_DIAGNOSIS"),
        ("CPU far below the limit", _down_cpu_report(cpu_peak=0.3), _starved_evidence(), "INSUFFICIENT_EVIDENCE"),
        ("CPU at 85%: busy, not starved", _down_cpu_report(cpu_peak=0.425), _starved_evidence(),
         "INSUFFICIENT_EVIDENCE"),
        ("ran with another limit", _down_cpu_report(), _starved_evidence(cpu="400m"), "INSUFFICIENT_EVIDENCE"),
    ]
    for name, report, evidence, code in cases:
        result, llm, _, _ = base.run_case(report=report, raw_payload=_raw(evidence), llm=base.MenuStubLLM())
        assert (result["reason_code"], bool(llm.prompts)) == (code, False), (name, result)
    print("✅ PASS: a down target gets a CPU increase only when running, not ready, unrestarted and CPU near the limit")


# ─── Proposer: Memory Leak ───

def test_leak_turns_the_feature_flag_off_and_nothing_else():
    llm = _llm()
    result, _, SF, _ = base.run_case(report=_leak_report(), raw_payload=_raw(_leak_evidence(), growth=30.0),
                                     llm=llm, manifest=FLAG_MANIFEST)
    assert result["status"] == pp.VALIDATED, (result["reason_code"], result.get("reason_detail"))
    row = base._rows(SF)[0]
    assert row.candidates["values"] == ["false"] and row.candidates["hard_cap"] is None
    assert row.proposed_values == {"op": "replace_scalar", "old_value": "true", "new_value": "false",
                                   "old_text": '"true"', "new_text": '"false"',
                                   "field_path": "spec.template.spec.containers[0].env[name=RESPONSE_CACHE_ENABLED].value"}
    assert row.previous_values == {"env_target-app_RESPONSE_CACHE_ENABLED": "true"}
    changed = [l for l in row.unified_diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    assert changed == ["-" + FLAG_LINE, '+          value: "false"'], changed
    assert row.model_info["prompt_version"] == "flag-decision/1"
    assert "MENU for the RESPONSE_CACHE_ENABLED feature flag: false" in llm.prompts[0]
    facts = {f["id"]: f["text"] for f in row.evidence["facts_offered"]}
    assert "30.0 MiB/min" in facts["F3"] and 'RESPONSE_CACHE_ENABLED="true"' in facts["F4"]
    # The real manifest with its current flag, which the pod also runs with.
    real_flag = next(e["value"] for e in _real_container()["env"] if e["name"] == "RESPONSE_CACHE_ENABLED")
    real, real_llm, SF2, _ = base.run_case(report=_leak_report(), llm=_llm(), manifest=REAL_MANIFEST,
                                           raw_payload=_raw(_leak_evidence(flag=real_flag), growth=30.0))
    if real_flag == "true":
        assert real["status"] == pp.VALIDATED and base._rows(SF2)[0].proposed_values["new_value"] == "false", real
    else:   # a merged leak remediation already turned it off: nothing left to turn off
        assert (real["status"], real["reason_code"], bool(real_llm.prompts)) == (
            pp.NOT_GENERATED, "INSUFFICIENT_EVIDENCE", False), real
    print(f"✅ PASS: leak + flag on -> VALIDATED one-line diff RESPONSE_CACHE_ENABLED \"true\" -> \"false\" "
          f"(real manifest: flag {real_flag!r})")


def test_flag_is_turned_off_only_for_a_measured_leak_in_that_feature():
    no_flag = base.MANIFEST_LF
    already_off = FLAG_MANIFEST.replace(FLAG_LINE, '          value: "false"')
    cases = [
        ("not growing", _leak_report(), _raw(_leak_evidence(), growth=4.9), FLAG_MANIFEST, "INSUFFICIENT_EVIDENCE"),
        ("growth unknown", _leak_report(), _raw(_leak_evidence(), growth=None), FLAG_MANIFEST, "INSUFFICIENT_EVIDENCE"),
        ("live flag off", _leak_report(), _raw(_leak_evidence(flag="false"), growth=30.0), FLAG_MANIFEST,
         "INSUFFICIENT_EVIDENCE"),
        ("no k8s evidence", _leak_report(), {"labels": cl.LABELS, "raw_context": {"telemetry": {
            "memory_growth_mib_per_min": 30.0}}}, FLAG_MANIFEST, "INSUFFICIENT_EVIDENCE"),
        ("CPU diagnosis", _leak_report(incident_type="HIGH_CPU"), _raw(_leak_evidence(), growth=30.0), FLAG_MANIFEST,
         "INCONSISTENT_DIAGNOSIS"),
        ("manifest already off", _leak_report(), _raw(_leak_evidence(), growth=30.0), already_off, "AT_BOUND"),
        ("manifest has no flag", _leak_report(), _raw(_leak_evidence(), growth=30.0), no_flag, "CURRENT_VALUE_MISSING"),
    ]
    for name, report, raw, manifest, code in cases:
        result, llm, _, _ = base.run_case(report=report, raw_payload=raw, llm=_llm(), manifest=manifest)
        assert (result["reason_code"], bool(llm.prompts)) == (code, False), (name, result)
    bad = base.StubLLM(response=base._decision(new_value="true", fact_ids=("F3",),
                                               runbook_ids=("k8s-oomkilled-memory-pressure",), rationale="keep it on"))
    result, _, _, _ = base.run_case(report=_leak_report(), raw_payload=_raw(_leak_evidence(), growth=30.0),
                                    llm=bad, manifest=FLAG_MANIFEST)
    assert (result["status"], result["reason_code"]) == (pp.REJECTED, "LLM_INVALID_CHOICE"), result
    # Defence in depth: even if the model's menu were wider, the bounds check refuses anything but "false".
    from unittest.mock import patch
    real_model = pp.build_decision_model
    with patch.object(pp, "build_decision_model", lambda f, r, c: real_model(f, r, c + ["true"])):
        result, _, _, _ = base.run_case(report=_leak_report(), raw_payload=_raw(_leak_evidence(), growth=30.0),
                                        llm=bad, manifest=FLAG_MANIFEST)
    assert (result["status"], result["reason_code"]) == (pp.REJECTED, "BOUNDS_VIOLATION"), result
    memory, llm, _, _ = base.run_case(report=base._report(), raw_payload=_raw(_leak_evidence(), growth=30.0),
                                      llm=base.MenuStubLLM())
    assert memory["reason_code"] == "LEAK_SUSPECTED" and not llm.prompts            # never a larger memory limit
    print("✅ PASS: flag off needs growth >= 5 MiB/min, the live flag on, a memory diagnosis and the flag in the "
          "manifest; the model can only pick \"false\"; a leak never gets more memory")


def test_flag_patch_primitives_are_bounded_and_reversible():
    import copy

    import yaml

    doc = yaml.safe_load(FLAG_MANIFEST)
    before = copy.deepcopy(doc)
    gen = PatchGenerator()
    patched, previous = gen.generate({"type": "DISABLE_FEATURE_FLAG", "flag": "RESPONSE_CACHE_ENABLED",
                                      "patch_value": "false"}, doc)
    assert doc == before and previous == {"env_target-app_RESPONSE_CACHE_ENABLED": "true"}
    assert patched["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] == "false"
    assert gen.apply_rollback(patched, previous) == before
    for action, manifest in (({"flag": "PATH", "patch_value": "false"}, doc),
                             ({"flag": "RESPONSE_CACHE_ENABLED", "patch_value": "true"}, doc),
                             ({"flag": "RESPONSE_CACHE_ENABLED", "patch_value": "false"}, patched),
                             ({"flag": "RESPONSE_CACHE_ENABLED", "patch_value": "false"}, yaml.safe_load(base.MANIFEST_LF))):
        try:
            gen.generate({"type": "DISABLE_FEATURE_FLAG", **action}, manifest)
            raise AssertionError(action)
        except PatchBoundsError:
            pass
    lines = FLAG_MANIFEST.split("\n")
    at = lines.index(FLAG_LINE) + 1
    for new in ("          value: false", '          value: "maybe"', '          name: "false"'):
        diff = make_unified_diff(base.REL, FLAG_MANIFEST, FLAG_MANIFEST.replace(FLAG_LINE, new))
        try:
            check_diff_policy(parse_unified_diff_strict(diff, base.REL), lines, at, "value")
            raise AssertionError(new)
        except PatchDiffError as exc:
            assert exc.code == "DIFF_POLICY_VIOLATION"
    for path, values in ((("spec.template.spec.containers[0].env[name=PATH].value"), ("true", "false")),
                         ("spec.template.spec.containers[0].env[name=RESPONSE_CACHE_ENABLED].value", ("false", "true")),
                         ("spec.template.spec.containers[0].env[name=RESPONSE_CACHE_ENABLED].value", ("true", "rm"))):
        try:
            dep.target_from_proposal({"kind": "Deployment", "namespace": "autofixops", "name": "autofixops-target-app",
                                      "container": "target-app"},
                                     {"op": "replace_scalar", "field_path": path, "old_value": values[0],
                                      "new_value": values[1]})
            raise AssertionError(path)
        except dep.DeployBlocked as exc:
            assert exc.code == "TARGET_INVALID"
    print("✅ PASS: flag patch: input untouched, exact rollback; other flags/values/lines refused by generator, "
          "diff policy and deployer")


def test_verification_windows_cover_the_delayed_faults():
    cfg = rv.VerifyConfig(health_url="http://x/health", interval_s=10, stabilization_s=60, timeout_s=300)
    t = lambda resource: dep.Target("autofixops", "autofixops-target-app", "target-app", resource, "a", "b")
    assert rv.windows_for(t("memory"), cfg) == (60, 300)
    assert rv.windows_for(t("cpu"), cfg) == (180, 330) and rv.windows_for(t("env"), cfg) == (180, 330)
    print("✅ PASS: CPU-limit and feature-flag fixes are verified for at least 180s (memory keeps the configured 60s)")


# ─── Stages 3-4, end to end with fakes ───

class ScenarioProm:
    def __init__(self, memory=60 * MIB, growth_mib_per_min=0.0, cpu=0.66, firing=False):
        self.memory, self.growth, self.cpu, self.firing, self.queries = memory, growth_mib_per_min, cpu, firing, []

    def __call__(self, query):
        self.queries.append(query)
        if query.startswith("ALERTS"):
            return [{"metric": {}, "value": [0, "1"]}] if self.firing else []
        if query.startswith("max(deriv(process_resident_memory_bytes"):
            return [{"metric": {}, "value": [0, str(self.growth * MIB / 60)]}]
        if "process_resident_memory_bytes" in query:
            return [{"metric": {}, "value": [0, str(self.memory)]}]
        if "process_cpu_seconds_total" in query:
            return [{"metric": {}, "value": [0, str(self.cpu)]}]
        return []


class ScenarioWorld(wft.World):
    """wft.World for any VALIDATED proposal: report, incident payload, manifest, alert and fakes."""

    def __init__(self, report, raw, manifest, alert, kube, prom, llm):
        self.SF = base._factory()
        self.incident_id = base._add_incident(self.SF, report=report, raw_payload=raw)
        db = self.SF()
        db.query(Incident).filter(Incident.id == self.incident_id).update({"alert_name": alert})
        db.commit()
        db.close()
        with base.temp_repo(manifest) as root:
            result = pp.run_for_incident(self.SF, self.incident_id, root, llm=llm.runnable, env={}, settings=base.FAST)
        assert result["status"] == pp.VALIDATED, result
        self.proposal_id = uuid.UUID(str(result["proposal_id"]))
        self.new_value = base._rows(self.SF)[0].proposed_values["new_value"]
        self.github = wft.FakeGitHub(manifest.encode("utf-8"))
        self.kube, self.prom, self.clock = kube, prom, wft.Clock()
        self.health, self.scheduled = 200, []
        self.env = dict(wft.ENV)
        self.deps = wf.Deps(github=lambda: (self.github, {"configured": True, "repo": "o/r", "missing": []}),
                            kube_runner=self.kube, http_get=lambda url: self.health, prom_query=self.prom,
                            schedule=lambda run_id, delay: self.scheduled.append(delay), env=self.env, now=self.clock)


def _flag_kube():
    kube = wft.FakeKube()
    kube.deployment["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "RESPONSE_CACHE_ENABLED", "value": "true"}]
    return kube


def _flag(kube):
    return kube.deployment["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"]


def _cpu(kube):
    return kube.deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"]


def _leak_world(prom):
    return ScenarioWorld(_leak_report(), _raw(_leak_evidence(), growth=30.0), FLAG_MANIFEST, "TargetAppMemoryLeak",
                         _flag_kube(), prom, _llm())


def _cpu_world(prom):
    return ScenarioWorld(_down_cpu_report(), _raw(_starved_evidence()), base.MANIFEST_LF, "TargetAppDown",
                         wft.FakeKube(), prom, base.MenuStubLLM())


def _verify(w, run_id, max_rounds=60):
    view = None
    for i in range(max_rounds):
        w.clock.tick(10)
        view = w.advance(run_id)
        if view["final"]:
            return view, (i + 1) * 10
    raise AssertionError(f"not final after {max_rounds} rounds: {view['stage']}")


def test_memory_leak_flow_turns_the_cache_off_and_resolves_after_memory_stops_growing():
    w = _leak_world(ScenarioProm(growth_mib_per_min=0.0))
    run_id = w.to_verifying()
    pr = w.github.prs[0]
    assert "DISABLE_FEATURE_FLAG: RESPONSE_CACHE_ENABLED flag true -> false" in pr["title"], pr["title"]
    assert b'value: "false"' in w.github.branches["main"][base.REL]                    # merged before deploying
    assert _flag(w.kube) == "false" and len(w.kube.applied) == 1
    assert {"op": "test", "path": "/spec/template/spec/containers/0/env/0/name",
            "value": "RESPONSE_CACHE_ENABLED"} in w.kube.applied[0]
    view, elapsed = _verify(w, run_id)
    assert view["stage"] == wf.RESOLVED, view["stage"]
    assert view["verification"]["stabilization_s"] == 180 and elapsed >= 60 + 180, elapsed
    checks = {c["name"]: c for c in view["verification"]["rounds"][-1]["checks"]}
    assert checks["flag_applied"]["ok"] and checks["memory_stable"]["ok"] and "0.0 MiB/min" in checks["memory_stable"]["detail"]
    assert "of the memory limit 100Mi" in checks["resource_headroom"]["detail"]
    early = view["verification"]["rounds"][0]["checks"]
    assert any(c["name"] == "memory_stable" and not c["ok"] and "collecting data" in c["detail"] for c in early)
    inc = w.incident()
    assert inc.status == IncidentStatus.RESOLVED
    notes = [e["note"] for e in inc.status_timeline if e.get("event") == "NOTE"]
    assert any("deployed RESPONSE_CACHE_ENABLED flag true -> false" in n for n in notes), notes
    print(f"✅ PASS: leak: approve → PR → CI → merge → flag off → memory stable → RESOLVED after {elapsed}s")


def test_memory_leak_flow_rolls_back_when_memory_keeps_growing():
    w = _leak_world(ScenarioProm(growth_mib_per_min=40.0))
    run_id = w.to_verifying()
    view, elapsed = _verify(w, run_id)
    assert view["stage"] == wf.ROLLED_BACK and elapsed >= 330, (view["stage"], elapsed)
    assert _flag(w.kube) == "true" and view["rollback"]["git_revert"]["created"]
    failing = [c["name"] for c in view["verification"]["rounds"][-1]["checks"] if not c["ok"]]
    assert failing == ["memory_stable"], failing
    assert w.incident().status == IncidentStatus.ESCALATED
    print("✅ PASS: leak still growing after the flag change → rolled back to \"true\", revert PR, ESCALATED")


def test_cpu_spike_flow_doubles_the_cpu_limit_and_resolves_after_the_longer_window():
    w = _cpu_world(ScenarioProm(cpu=0.66))
    run_id = w.to_verifying()
    assert _cpu(w.kube) == "1" and "INCREASE_CPU_LIMIT: cpu limit 500m -> 1" in w.github.prs[0]["title"]
    view, elapsed = _verify(w, run_id)
    assert view["stage"] == wf.RESOLVED and elapsed >= 180, (view["stage"], elapsed)
    checks = {c["name"]: c for c in view["verification"]["rounds"][-1]["checks"]}
    assert "66% of the new cpu limit 1" in checks["resource_headroom"]["detail"] and "memory_stable" not in checks
    assert w.incident().status == IncidentStatus.RESOLVED
    print(f"✅ PASS: CPU spike: approve → PR → CI → merge → 500m→1 → RESOLVED after {elapsed}s (>= 180s)")


def test_cpu_spike_flow_rolls_back_when_still_starved():
    w = _cpu_world(ScenarioProm(cpu=0.97))
    run_id = w.to_verifying()
    view, _ = _verify(w, run_id)
    assert view["stage"] == wf.ROLLED_BACK and _cpu(w.kube) == "500m"
    assert w.incident().status == IncidentStatus.ESCALATED
    print("✅ PASS: CPU still at the new limit → rolled back to 500m, ESCALATED")


def test_no_step_is_skipped_for_the_new_remediations():
    for make in (lambda: _leak_world(ScenarioProm()), lambda: _cpu_world(ScenarioProm())):
        w = make()
        run, _ = w.approve()
        assert w.advance(run["id"])["stage"] == wf.PR_OPEN and w.kube.applied == []
        w.github.ci = [{"name": "CI", "status": "in_progress", "conclusion": None, "url": "u"}]
        w.github.merge(1)
        assert w.advance(run["id"])["stage"] == wf.PR_OPEN and w.kube.applied == []       # CI still running
        w.github.ci = [{"name": "CI", "status": "completed", "conclusion": "success", "url": "u"}]
        assert w.advance(run["id"])["stage"] == wf.READY_TO_DEPLOY and w.kube.applied == []
        assert w.incident().status != IncidentStatus.RESOLVED
    print("✅ PASS: flag and CPU remediations still wait for approval, PR, CI and merge before any deployment")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
