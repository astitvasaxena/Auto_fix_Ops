"""
Stages 3-4 drill: approval -> GitHub PR -> controlled deployment -> recovery verification.
========================================================================================
Every test starts from a genuinely VALIDATED Stage 2 proposal (real proposer,
stub LLM, temporary repo) and drives engine.remediation_workflow with fakes:
an in-memory GitHub, a fake kubectl that applies RFC 6902 patches to a
Deployment object, and fake Prometheus/HTTP probes. No network, cluster,
GitHub token, Celery or Postgres is used.

Run: python tests/remediation/test_remediation_workflow.py   (or under pytest)
"""
import copy
import datetime
import json
import os
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "ai"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_patch_proposals as base
from api import remediation as api_module
from api.models import Incident, IncidentStatus, RemediationRun, SystemConfig, SystemMode
from engine import deployer as dep
from engine import patch_proposer as pp
from engine import remediation_workflow as wf
from engine.patch_safety import git_blob_sha

ENV = {
    "SHADOW_MODE": "false", "REMEDIATION_REQUIRE_PR": "true", "DEPLOY_MODE": "kubectl",
    "DEPLOY_KUBE_CONTEXT": "docker-desktop", "DEPLOY_ALLOWED_TARGETS": "autofixops/autofixops-target-app",
    "VERIFY_HEALTH_URL": "http://localhost:30081/health", "VERIFY_INTERVAL_SECONDS": "10",
    "VERIFY_STABILIZATION_SECONDS": "30", "VERIFY_TIMEOUT_SECONDS": "120", "REMEDIATION_PR_POLL_SECONDS": "30",
}
ACTOR = {"authenticated": True, "identity": "alice", "source": "approver-token"}


# ─── Fakes ───

class FakeGitHub:
    """In-memory repository: branches map to {path: bytes}; PRs are dicts."""

    def __init__(self, manifest: bytes):
        self.branches = {"main": {base.REL: manifest}}
        self.commits = {"c0": {base.REL: manifest}}
        self.heads = {"main": "c0"}
        self.prs, self.calls = [], []

    def _commit(self, branch, files):
        sha = f"c{len(self.commits)}"
        self.commits[sha] = dict(files)
        self.heads[branch] = sha
        self.branches[branch] = dict(files)
        return sha

    def default_branch(self):
        return "main"

    def get_file(self, path, ref):
        files = self.commits.get(ref) or self.branches[ref]
        data = files[path]
        return data, git_blob_sha(data)

    def branch_head(self, branch):
        return self.heads.get(branch)

    def create_branch(self, branch, from_sha):
        self.calls.append(("create_branch", branch))
        self.heads[branch] = from_sha
        self.branches[branch] = dict(self.commits[from_sha])

    def put_file(self, path, content, base_blob_sha, branch, message):
        self.calls.append(("put_file", branch, message))
        assert git_blob_sha(self.branches[branch][path]) == base_blob_sha, "stale blob sha"
        return self._commit(branch, {**self.branches[branch], path: content})

    def find_pr(self, head_branch):
        return next((dict(p) for p in self.prs if p["head_ref"] == head_branch), None)

    def create_pr(self, title, body, head, base, draft):
        self.calls.append(("create_pr", head))
        pr = {"number": len(self.prs) + 1, "url": f"https://github.test/pr/{len(self.prs) + 1}", "state": "open",
              "merged": False, "draft": draft, "head_ref": head, "head_sha": self.heads[head], "base_ref": base,
              "merge_commit_sha": None, "title": title, "body": body}
        self.prs.append(pr)
        return dict(pr)

    def get_pr(self, number):
        return dict(self.prs[number - 1])

    def merge(self, number, content=None):
        pr = self.prs[number - 1]
        files = dict(self.branches[pr["head_ref"]])
        if content is not None:
            files[base.REL] = content
        pr.update(state="closed", merged=True, merge_commit_sha=self._commit("main", files))


class FakeKube:
    """Fake kubectl: a Deployment object, RFC 6902 'test'/'replace', rollout and pods."""

    def __init__(self, memory="100Mi", rollout_ok=True, healthy=True):
        self.deployment = {
            "metadata": {"name": "autofixops-target-app", "namespace": "autofixops", "generation": 1,
                         "resourceVersion": "100"},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "target-app", "system": "autofixops"}},
                     "template": {"spec": {"containers": [{
                         "name": "target-app", "image": "autofixops/target-app:latest",
                         "resources": {"limits": {"memory": memory, "cpu": "500m"},
                                       "requests": {"memory": "64Mi", "cpu": "100m"}}}]}}},
            "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1,
                       "availableReplicas": 1},
        }
        self.rollout_ok, self.healthy, self.restarts = rollout_ok, healthy, 0
        self.argvs, self.applied = [], []

    def _resolve(self, path):
        parts = path.strip("/").split("/")
        node = self.deployment
        for p in parts[:-1]:
            node = node[int(p)] if isinstance(node, list) else node[p]
        return node, parts[-1]

    def __call__(self, argv, timeout):
        self.argvs.append(argv)
        assert argv[1:5] == ["--context", "docker-desktop", "--namespace", "autofixops"], argv
        verb = argv[5:]
        if verb[:2] == ["get", "deployment"]:
            return dep.CommandResult(0, json.dumps(self.deployment), "")
        if verb[:2] == ["get", "pods"]:
            state = {} if self.healthy else {"waiting": {"reason": "CrashLoopBackOff"}}
            pod = {"metadata": {"name": "autofixops-target-app-abc"},
                   "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True" if self.healthy else "False"}],
                              "containerStatuses": [{"restartCount": self.restarts, "state": state}]}}
            return dep.CommandResult(0, json.dumps({"items": [pod]}), "")
        if verb[0] == "patch":
            ops = json.loads(verb[verb.index("--patch") + 1])
            trial = copy.deepcopy(self.deployment)
            saved, self.deployment = self.deployment, trial
            for op in ops:
                node, key = self._resolve(op["path"])
                current = node[int(key)] if isinstance(node, list) else node.get(key)
                if op["op"] == "test" and current != op["value"]:
                    self.deployment = saved
                    return dep.CommandResult(1, "", "The request is invalid: the server rejected our request "
                                                    "due to an error in our request (test operation failed)")
                if op["op"] == "replace":
                    node[key] = op["value"]
            if "--dry-run=server" in verb:
                self.deployment = saved
            else:
                trial["metadata"]["generation"] += 1
                trial["status"]["observedGeneration"] = trial["metadata"]["generation"]
                self.applied.append(ops)
            return dep.CommandResult(0, "deployment.apps/autofixops-target-app patched", "")
        if verb[:2] == ["rollout", "status"]:
            if self.rollout_ok:
                return dep.CommandResult(0, 'deployment "autofixops-target-app" successfully rolled out', "")
            return dep.CommandResult(1, "", "error: timed out waiting for the condition")
        raise AssertionError(f"unexpected kubectl call {argv}")

    @property
    def memory(self):
        return self.deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]


class FakeProm:
    def __init__(self, firing=False, usage=50 * 1024 ** 2):
        self.firing, self.usage, self.queries = firing, usage, []

    def __call__(self, query):
        self.queries.append(query)
        if query.startswith("ALERTS"):
            return [{"metric": {"alertname": "HighMemoryUsage"}, "value": [0, "1"]}] if self.firing else []
        if "process_resident_memory_bytes" in query:
            return [{"metric": {}, "value": [0, str(self.usage)]}]
        return []


class Clock:
    def __init__(self):
        self.t = datetime.datetime(2026, 10, 31, 12, 0, 0)

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += datetime.timedelta(seconds=seconds)


class World:
    """One VALIDATED proposal plus fakes wired into wf.Deps."""

    def __init__(self, env=None, github=True, kube=None, prom=None, health=200):
        self.SF = base._factory()
        self.incident_id = base._add_incident(self.SF, report=base._report(confidence=0.9),
                                              raw_payload={"labels": {"alertname": "HighMemoryUsage",
                                                                      "namespace": "autofixops"}})
        db = self.SF()
        db.query(Incident).filter(Incident.id == self.incident_id).update({"alert_name": "HighMemoryUsage"})
        db.commit()
        db.close()
        with base.temp_repo() as root:
            result = pp.run_for_incident(self.SF, self.incident_id, root, llm=base.StubLLM().runnable, env={},
                                         settings=base.FAST)
        assert result["status"] == pp.VALIDATED, result
        self.proposal_id = uuid.UUID(str(result["proposal_id"]))
        self.new_value = base._rows(self.SF)[0].proposed_values["new_value"]   # chosen from the menu by the stub
        self.github = FakeGitHub(base.MANIFEST_LF.encode("utf-8")) if github else None
        self.kube, self.prom, self.clock = kube or FakeKube(), prom or FakeProm(), Clock()
        self.health, self.scheduled = health, []
        self.env = dict(ENV, **(env or {}))
        self.deps = wf.Deps(github=lambda: (self.github, {"configured": bool(self.github), "repo": "o/r",
                                                          "missing": [] if self.github else ["token"]}),
                            kube_runner=self.kube, http_get=lambda url: self.health, prom_query=self.prom,
                            schedule=lambda run_id, delay: self.scheduled.append(delay), env=self.env, now=self.clock)

    def approve(self, actor=ACTOR):
        run, changed = wf.decide(self.SF, self.incident_id, self.proposal_id, wf.APPROVED, actor, "looks right",
                                 now=self.clock())
        return run, changed

    def advance(self, run_id):
        return wf.advance(self.SF, uuid.UUID(run_id), self.deps)

    def incident(self):
        db = self.SF()
        try:
            return db.query(Incident).filter(Incident.id == self.incident_id).first()
        finally:
            db.close()

    def runs(self):
        db = self.SF()
        try:
            return db.query(RemediationRun).all()
        finally:
            db.close()

    def to_verifying(self):
        run, _ = self.approve()
        self.advance(run["id"])                     # PR opened
        self.github.merge(1)
        self.advance(run["id"])                     # merge seen -> READY_TO_DEPLOY
        view = self.advance(run["id"])              # deploy -> VERIFYING
        assert view["stage"] == wf.VERIFYING, view
        return run["id"]


# ─── Tests ───

def test_full_flow_pr_deploy_verify_resolved():
    w = World()
    run, changed = w.approve()
    assert changed and run["stage"] == wf.APPROVED
    assert w.incident().status == IncidentStatus.POLICY_APPROVED

    view = w.advance(run["id"])
    assert view["stage"] == wf.PR_OPEN, view
    assert w.incident().status == IncidentStatus.PENDING_PR_MERGE
    pr = w.github.prs[0]
    assert pr["draft"] is False and not pr["title"].startswith("[SHADOW]")
    for text in ("HighMemoryUsage", "100Mi", w.new_value, "Validation results", "APPROVED", "alice", "```diff"):
        assert text in pr["body"], text
    patched = w.github.branches[pr["head_ref"]][base.REL]
    assert f'memory: "{w.new_value}"'.encode() in patched and patched.count(b"\n") == base.MANIFEST_LF.count("\n")
    assert w.kube.argvs == []                       # nothing deployed before the merge

    assert w.advance(run["id"])["stage"] == wf.PR_OPEN      # not merged yet: wait
    w.github.merge(1)
    assert w.advance(run["id"])["stage"] == wf.READY_TO_DEPLOY

    view = w.advance(run["id"])
    assert view["stage"] == wf.VERIFYING, view
    assert w.kube.memory == w.new_value
    assert len(w.kube.applied) == 1
    dry = [a for a in w.kube.argvs if "--dry-run=server" in a]
    real = [a for a in w.kube.argvs if a[5] == "patch" and "--dry-run=server" not in a]
    assert w.kube.argvs.index(dry[0]) < w.kube.argvs.index(real[0])
    assert view["deployment"]["previous"]["container"]["resources"]["limits"]["memory"] == "100Mi"

    for _ in range(3):                               # rounds at t+10, +20, +30, +40
        w.clock.tick(10)
        view = w.advance(run["id"])
    assert view["stage"] == wf.VERIFYING, "must stay stable for the whole window"
    w.clock.tick(10)
    view = w.advance(run["id"])
    assert view["stage"] == wf.RESOLVED, view
    assert view["verification"]["result"] == "PASSED"
    assert all(c["ok"] for c in view["verification"]["rounds"][-1]["checks"])
    inc = w.incident()
    assert inc.status == IncidentStatus.RESOLVED and inc.resolved_at is not None
    assert [e["status"] for e in inc.status_timeline][-5:] == [
        "POLICY_APPROVED", "REMEDIATING", "PENDING_PR_MERGE", "VERIFIED", "RESOLVED"]
    assert w.runs()[0].deploy_lock is None
    db = w.SF()
    timeline = wf.incident_timeline(db, db.query(Incident).first())
    db.close()
    kinds = [i["kind"] for i in timeline]
    for k in ("ALERT_RECEIVED", "PROPOSAL_VALIDATED", "REMEDIATION_HUMAN_APPROVED", "REMEDIATION_PR_OPENED",
              "REMEDIATION_PR_MERGED", "REMEDIATION_DEPLOYED", "REMEDIATION_VERIFIED", "STATUS_RESOLVED"):
        assert k in kinds, k
    print("✅ PASS: approve → PR → merge → dry-run + patch → stable verification → RESOLVED, full timeline")


def test_duplicate_approvals_and_steps_create_one_run_and_one_pr():
    w = World()
    first, changed1 = w.approve()
    second, changed2 = w.approve()
    assert changed1 and not changed2 and first["id"] == second["id"]
    w.advance(first["id"])
    w.advance(first["id"])                           # PR already open: only polls
    db = w.SF()
    db.query(RemediationRun).update({"stage": wf.APPROVED})   # simulate a replayed PR step
    db.commit()
    db.close()
    w.advance(first["id"])                           # adopts the existing PR
    assert len(w.github.prs) == 1 and [c[0] for c in w.github.calls].count("create_pr") == 1
    assert len(w.runs()) == 1
    print("✅ PASS: duplicate approvals/steps → one run, one branch commit, one PR")


def test_concurrent_claim_is_refused():
    w = World()
    run, _ = w.approve()
    _, token = wf._claim(w.SF, uuid.UUID(run["id"]), w.clock())
    assert token
    _, token2 = wf._claim(w.SF, uuid.UUID(run["id"]), w.clock())
    assert token2 is None                            # second worker does nothing
    view = w.advance(run["id"])
    assert view["stage"] == wf.APPROVED and w.github.prs == []
    w.clock.tick(wf.STEP_STALE_S + 1)                # a dead worker's claim expires
    assert w.advance(run["id"])["stage"] == wf.PR_OPEN
    print("✅ PASS: a claimed step cannot run twice; stale claims can be recovered")


def test_one_deployment_per_target():
    w = World()
    run_id = w.to_verifying()
    assert w.runs()[0].deploy_lock is True
    db = w.SF()
    other = RemediationRun(incident_id=w.incident_id, proposal_id=uuid.uuid4(), report_sha256="x", diff_sha256="y",
                           target_key=w.runs()[0].target_key, stage=wf.READY_TO_DEPLOY, audit_log=[])
    db.add(other)
    db.commit()
    other_id = other.id
    db.close()
    assert wf._set_lock(w.SF, other_id, True) is False
    print("✅ PASS: the database lock allows one deploying/verifying run per target")


def test_verification_failure_rolls_back_and_opens_revert_pr():
    w = World(prom=FakeProm(firing=True))
    run_id = w.to_verifying()
    view = None
    for _ in range(13):
        w.clock.tick(10)
        view = w.advance(run_id)
        if view["final"]:
            break
    assert view["stage"] == wf.ROLLED_BACK, view["stage"]
    assert w.kube.memory == "100Mi"                  # previous value restored
    assert view["rollback"]["ok"] and view["rollback"]["git_revert"]["created"]
    assert w.github.branches["autofixops/revert-" + str(w.proposal_id)[:8]][base.REL] == base.MANIFEST_LF.encode()
    assert w.incident().status == IncidentStatus.FAILED
    assert w.runs()[0].deploy_lock is None
    failing = [c["name"] for c in view["verification"]["rounds"][-1]["checks"] if not c["ok"]]
    assert "alert_cleared" in failing
    print("✅ PASS: verification never stable → inverse patch restores 100Mi, revert PR, incident FAILED")


def test_rollout_failure_rolls_back():
    w = World(kube=FakeKube(rollout_ok=False))
    run, _ = w.approve()
    w.advance(run["id"])
    w.github.merge(1)
    w.advance(run["id"])
    view = w.advance(run["id"])
    assert view["stage"] == wf.ROLLBACK_FAILED or view["stage"] == wf.ROLLED_BACK
    # the fake's rollout keeps failing, so the rollback rollout also fails: reported, never hidden
    assert view["stage"] == wf.ROLLBACK_FAILED and w.kube.memory == "100Mi"
    assert w.incident().status == IncidentStatus.FAILED
    print("✅ PASS: rollout failure → value restored; a failing rollback rollout is reported as ROLLBACK_FAILED")


def test_drift_is_refused_without_any_change():
    w = World(kube=FakeKube(memory="110Mi"))
    run, _ = w.approve()
    w.advance(run["id"])
    w.github.merge(1)
    w.advance(run["id"])
    view = w.advance(run["id"])
    assert view["stage"] == wf.FAILED and view["deployment"]["error"] == "DRIFT_DETECTED"
    assert w.kube.applied == [] and w.kube.memory == "110Mi"
    assert not any(a[5] == "patch" for a in w.kube.argvs)
    print("✅ PASS: live value differs from the proposal → refused before any patch")


def test_change_between_read_and_patch_is_refused_by_the_server():
    class RacingKube(FakeKube):
        """Someone edits the limit right after AutoFixOps read the deployment."""
        def __call__(self, argv, timeout):
            result = super().__call__(argv, timeout)
            if argv[5:7] == ["get", "deployment"] and not getattr(self, "raced", False):
                self.raced = True
                self.deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "300Mi"
            return result

    w = World(kube=RacingKube())
    run, _ = w.approve()
    w.advance(run["id"])
    w.github.merge(1)
    w.advance(run["id"])
    view = w.advance(run["id"])
    assert view["stage"] == wf.FAILED, view["stage"]
    assert w.kube.memory == "300Mi" and w.kube.applied == []    # the other change was not overwritten
    print("✅ PASS: JSON-patch 'test' ops make the API server refuse a racing change atomically")


def test_merged_content_must_equal_the_approved_patch():
    w = World()
    run, _ = w.approve()
    w.advance(run["id"])
    edited = base.MANIFEST_LF.replace('memory: "100Mi"', 'memory: "4Gi"').encode()
    w.github.merge(1, content=edited)
    view = w.advance(run["id"])
    assert view["stage"] == wf.FAILED and w.kube.argvs == []
    print("✅ PASS: a PR edited before merge is never deployed")


def test_pr_closed_or_base_changed_fails_safely():
    w = World()
    run, _ = w.approve()
    w.advance(run["id"])
    w.github.prs[0].update(state="closed")
    assert w.advance(run["id"])["stage"] == wf.FAILED
    assert w.incident().status == IncidentStatus.FAILED

    w2 = World()
    w2.github.branches["main"][base.REL] = w2.github.commits["c0"][base.REL] = b"kind: Changed\n"
    run2, _ = w2.approve()
    view = w2.advance(run2["id"])
    assert view["stage"] == wf.FAILED and w2.github.prs == []
    assert any(e.get("code") == "BASE_CHANGED" for e in view["audit_log"])
    print("✅ PASS: PR closed unmerged → FAILED; manifest changed on main → no PR")


def test_blocked_steps_change_nothing_and_can_resume():
    w = World(github=False)
    run, _ = w.approve()
    view = w.advance(run["id"])
    assert view["stage"] == wf.APPROVED and view["blocked_code"] == "GITHUB_NOT_CONFIGURED"

    w.env["REMEDIATION_REQUIRE_PR"] = "false"
    w.env["DEPLOY_MODE"] = "disabled"
    view = w.advance(run["id"])
    assert view["stage"] == wf.READY_TO_DEPLOY and view["github"]["mode"] == "skipped"
    view = w.advance(run["id"])
    assert view["stage"] == wf.READY_TO_DEPLOY and view["blocked_code"] == "DEPLOY_DISABLED" and w.kube.argvs == []

    w.env.update(DEPLOY_MODE="kubectl", DEPLOY_ALLOWED_TARGETS="autofixops/other-app")
    assert w.advance(run["id"])["blocked_code"] == "TARGET_NOT_ALLOWLISTED" and w.kube.argvs == []
    w.env.update(DEPLOY_ALLOWED_TARGETS=ENV["DEPLOY_ALLOWED_TARGETS"], SHADOW_MODE="true")
    assert w.advance(run["id"])["blocked_code"] == "SHADOW_MODE" and w.kube.argvs == []
    w.env["SHADOW_MODE"] = "false"
    view = w.advance(run["id"])
    assert view["stage"] == wf.VERIFYING and w.kube.memory == w.new_value
    assert w.incident().status == IncidentStatus.REMEDIATING
    w.clock.tick(10); w.advance(run["id"])
    w.clock.tick(30); view = w.advance(run["id"])
    assert view["stage"] == wf.RESOLVED and w.incident().status == IncidentStatus.RESOLVED
    print("✅ PASS: no GitHub / deploy disabled / not allowlisted / shadow → blocked, nothing changed; resumes later")


def test_kill_switch_refuses_approval_and_blocks_steps():
    w = World()
    db = w.SF()
    db.merge(SystemConfig(id="global", system_mode=SystemMode.DISABLED))
    db.commit()
    db.close()
    try:
        w.approve()
        raise AssertionError("approval accepted while DISABLED")
    except wf.WorkflowError as exc:
        assert exc.code == "SYSTEM_DISABLED"
    db = w.SF()
    db.merge(SystemConfig(id="global", system_mode=SystemMode.ACTIVE))
    db.commit()
    run, _ = w.approve()
    db.merge(SystemConfig(id="global", system_mode=SystemMode.DISABLED))
    db.commit()
    db.close()
    assert w.advance(run["id"])["blocked_code"] == "SYSTEM_DISABLED" and w.github.prs == []
    print("✅ PASS: kill switch refuses approvals and blocks every step")


def test_only_latest_intact_validated_proposal_can_be_approved():
    w = World()
    try:
        wf.decide(w.SF, w.incident_id, uuid.uuid4(), wf.APPROVED, ACTOR)
        raise AssertionError("unknown proposal approved")
    except wf.WorkflowError as exc:
        assert exc.code == "PROPOSAL_NOT_LATEST"
    db = w.SF()
    from api.models import PatchProposal
    db.query(PatchProposal).update({"unified_diff": "--- tampered\n"})
    db.commit()
    db.close()
    try:
        w.approve()
        raise AssertionError("tampered proposal approved")
    except wf.WorkflowError as exc:
        assert exc.code == "PROPOSAL_NOT_VALIDATED"     # integrity check reports FAILED/INTEGRITY_MISMATCH
    print("✅ PASS: only the latest, integrity-checked VALIDATED proposal can be approved")


def test_reject_and_manual_close():
    w = World()
    try:
        wf.decide(w.SF, w.incident_id, w.proposal_id, wf.REJECTED, ACTOR, "")
        raise AssertionError("rejection without reason")
    except wf.WorkflowError as exc:
        assert exc.code == "REASON_REQUIRED"
    run, changed = wf.decide(w.SF, w.incident_id, w.proposal_id, wf.REJECTED, ACTOR, "wrong fix")
    assert changed and run["stage"] == wf.REJECTED and run["final"]
    assert w.incident().status == IncidentStatus.ESCALATED
    assert w.advance(run["id"])["stage"] == wf.REJECTED and w.github is not None and w.github.prs == []
    wf.close_incident(w.SF, w.incident_id, ACTOR, "fixed by hand")
    inc = w.incident()
    assert inc.status == IncidentStatus.RESOLVED and "closed manually by alice" in inc.status_timeline[-1]["note"]
    print("✅ PASS: rejection needs a reason, creates no PR; manual close is recorded")


def test_kubectl_arguments_are_fixed_and_validated():
    for bad in ({"namespace": "autofixops; rm -rf /"}, {"container": "$(whoami)"}, {"name": "a b"}):
        ref = {"kind": "Deployment", "namespace": "autofixops", "name": "autofixops-target-app",
               "container": "target-app", "field_path": "spec.template.spec.containers[0].resources.limits.memory",
               **bad}
        try:
            dep.target_from_proposal(ref, {"op": "replace_scalar", "old_value": "100Mi", "new_value": "125Mi",
                                           "field_path": ref["field_path"]})
            raise AssertionError(f"accepted {bad}")
        except dep.DeployBlocked as exc:
            assert exc.code == "TARGET_INVALID"
    for env in ({"DEPLOY_MODE": "kubectl"}, {"DEPLOY_MODE": "kubectl", "DEPLOY_KUBE_CONTEXT": "x"},
                {"DEPLOY_MODE": "shell", "DEPLOY_KUBE_CONTEXT": "x", "DEPLOY_ALLOWED_TARGETS": "a/b"}):
        try:
            dep.deploy_config(env)
            raise AssertionError(f"accepted {env}")
        except dep.DeployBlocked as exc:
            assert exc.code == "DEPLOY_CONFIG_INVALID"
    cfg = dep.deploy_config({"DEPLOY_MODE": "kubectl", "DEPLOY_KUBE_CONTEXT": "x", "DEPLOY_ALLOWED_TARGETS": "kube-system/dns"})
    try:
        dep.check_allowed(cfg, dep.Target("kube-system", "dns", "c", "memory", "1Mi", "2Mi"))
        raise AssertionError("restricted namespace allowed")
    except dep.DeployBlocked as exc:
        assert exc.code == "RESTRICTED_NAMESPACE"

    w = World()
    w.to_verifying()
    for argv in w.kube.argvs:
        assert argv[0] == "kubectl" and all(isinstance(a, str) for a in argv)
        if "--patch" in argv:
            ops = json.loads(argv[argv.index("--patch") + 1])
            assert [o["op"] for o in ops] == ["test", "test", "replace"]
            assert {o["path"] for o in ops} <= {"/spec/template/spec/containers/0/name",
                                                 "/spec/template/spec/containers/0/resources/limits/memory"}
    print("✅ PASS: kubectl gets fixed verbs and validated values only; patch touches one field")


def _client(w, host="127.0.0.1"):
    app = FastAPI()
    app.include_router(api_module.router)
    app.dependency_overrides[api_module.get_session_factory] = lambda: w.SF
    dispatched = []
    app.dependency_overrides[api_module.get_dispatcher] = lambda: dispatched.append
    return TestClient(app, client=(host, 50000)), dispatched


def test_api_approver_auth_and_json_only():
    w = World()
    body = {"proposal_id": str(w.proposal_id), "reason": "ok"}
    url = f"/api/v1/incidents/{w.incident_id}/remediation/approve"
    remote, _ = _client(w, host="10.0.0.5")
    assert remote.post(url, json=body).status_code == 403          # remote, no tokens configured
    local, dispatched = _client(w)
    assert local.post(url, data=body).status_code == 415          # form post (cross-site style)
    assert local.post(url, json={**body, "confidence": 1.0}).status_code == 422   # client fields refused

    os.environ["APPROVER_TOKENS"] = "alice:0123456789abcdef0123"
    try:
        assert local.post(url, json=body).status_code == 401
        assert local.post(url, json=body, headers={"Authorization": "Bearer wrong-token-000000"}).status_code == 401
        resp = local.post(url, json=body, headers={"Authorization": "Bearer 0123456789abcdef0123"})
        assert resp.status_code == 200, resp.text
        run = resp.json()["run"]
        assert run["decided_by"]["authenticated"] is True and run["decided_by"]["identity"] == "alice"
        assert dispatched == [run["id"]]
        assert local.get(f"/api/v1/incidents/{w.incident_id}/remediation").json()["approver_auth"] == "token"
    finally:
        del os.environ["APPROVER_TOKENS"]
    view = local.get(f"/api/v1/incidents/{w.incident_id}/remediation").json()
    assert view["run"]["stage"] == wf.APPROVED and not view["can_decide"] and view["approver_auth"] == "local-only"
    print("✅ PASS: remote approvals refused without tokens; token identity recorded; JSON only; no extra fields")


def test_api_local_approval_is_recorded_unauthenticated():
    w = World()
    local, dispatched = _client(w)
    resp = local.post(f"/api/v1/incidents/{w.incident_id}/remediation/approve",
                      json={"proposal_id": str(w.proposal_id)})
    assert resp.status_code == 200, resp.text
    who = resp.json()["run"]["decided_by"]
    assert who["authenticated"] is False and who["identity"] is None and who["client_host"] == "127.0.0.1"
    print("✅ PASS: local demo approval is recorded as unauthenticated, with no invented identity")


if __name__ == "__main__":
    print("\n" + "=" * 60 + "\nSTAGES 3-4: approval, PR, deployment, verification\n" + "=" * 60 + "\n")
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\n🎯 ALL CHECKS PASSED\n")
