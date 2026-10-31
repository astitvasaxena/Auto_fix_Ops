"""
Controlled deployment for Stage 4.
==================================
Applies ONE approved, validated change - a resource limit, or turning off an
allowlisted feature flag (container env value "true" -> "false") - to ONE
allowlisted Kubernetes Deployment, and can undo it exactly.

How it stays controlled:
  - Off unless configured: DEPLOY_MODE=kubectl plus an explicit DEPLOY_KUBE_CONTEXT
    and DEPLOY_ALLOWED_TARGETS ("namespace/deployment" list). Protected
    namespaces (engine.policy.RESTRICTED_NAMESPACES) are always refused.
  - The change is an RFC 6902 JSON patch built by code from the stored proposal
    (container name, old value, new value). Its "test" operations make the API
    server reject the whole patch if the live container name or the live limit
    is not what the proposal was validated against (drift), so nothing partial
    is ever written. A server-side dry run runs first.
  - kubectl is executed with an argument list (never a shell). Every argument
    is either a fixed word or a value checked against a strict pattern; no text
    produced by an LLM is ever passed to it.
  - The previous container spec is recorded before the change; rollback is the
    inverse JSON patch (test the new value, put the old one back).
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from engine.patch_safety import FEATURE_FLAGS
from engine.policy import RESTRICTED_NAMESPACES

_K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_CONTEXT = re.compile(r"^[A-Za-z0-9._@:/-]{1,128}$")
_QUANTITY = re.compile(r"^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|K|M|G)?$")
_SERVICE_PROXY = re.compile(r"^/api/v1/namespaces/[a-z0-9-]{1,63}/services/[a-z0-9-]{1,63}:[0-9]{1,5}/proxy/[A-Za-z0-9._/-]{0,200}$")
_POD_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_FIELD_PATH = re.compile(r"^spec\.template\.spec\.containers\[\d+\]\.resources\.limits\.(memory|cpu)$")
_FLAG_PATH = re.compile(r"^spec\.template\.spec\.containers\[\d+\]\.env\[name=([A-Z][A-Z0-9_]{0,62})\]\.value$")
MAX_OUTPUT = 2000


class DeployBlocked(Exception):
    """The step cannot run under the current configuration or cluster state. Nothing was changed."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class DeployFailed(Exception):
    """The change was attempted and did not succeed. `applied` says whether the cluster may have changed."""

    def __init__(self, code: str, detail: str, applied: bool, record: dict):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.applied, self.record = code, detail, applied, record


@dataclass(frozen=True)
class DeployConfig:
    kubectl: str
    context: str
    allowed_targets: frozenset
    rollout_timeout_s: int


def _int_env(env, name, default, low, high) -> int:
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        raise DeployBlocked("DEPLOY_CONFIG_INVALID", f"{name} is not an integer")
    if not low <= value <= high:
        raise DeployBlocked("DEPLOY_CONFIG_INVALID", f"{name} must be between {low} and {high}")
    return value


def deploy_config(env=None) -> DeployConfig:
    """Reads and validates the deployment settings. Raises DeployBlocked when deployment is off or invalid."""
    env = os.environ if env is None else env
    mode = (env.get("DEPLOY_MODE") or "disabled").strip().lower()
    if mode == "disabled":
        raise DeployBlocked("DEPLOY_DISABLED", "automated deployment is off (set DEPLOY_MODE=kubectl to enable)")
    if mode != "kubectl":
        raise DeployBlocked("DEPLOY_CONFIG_INVALID", f"unknown DEPLOY_MODE {mode!r}")
    return read_config(env)


def read_config(env=None) -> DeployConfig:
    """Cluster settings for READ-ONLY use (evidence collection): the same explicit context and
    target allowlist as deployment, but valid while deployment itself is disabled."""
    env = os.environ if env is None else env
    context = (env.get("DEPLOY_KUBE_CONTEXT") or "").strip()
    if not _CONTEXT.match(context):
        raise DeployBlocked("DEPLOY_CONFIG_INVALID", "DEPLOY_KUBE_CONTEXT must name the kubectl context explicitly")
    targets = set()
    for item in (env.get("DEPLOY_ALLOWED_TARGETS") or "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split("/")
        if len(parts) != 2 or not all(_K8S_NAME.match(p) for p in parts):
            raise DeployBlocked("DEPLOY_CONFIG_INVALID", f"DEPLOY_ALLOWED_TARGETS entry {item!r} is not namespace/name")
        targets.add(item)
    if not targets:
        raise DeployBlocked("DEPLOY_CONFIG_INVALID", "DEPLOY_ALLOWED_TARGETS is empty")
    kubectl = (env.get("KUBECTL_PATH") or "kubectl").strip()
    if not kubectl or any(c in kubectl for c in "\r\n\0"):
        raise DeployBlocked("DEPLOY_CONFIG_INVALID", "KUBECTL_PATH is invalid")
    return DeployConfig(kubectl=kubectl, context=context, allowed_targets=frozenset(targets),
                        rollout_timeout_s=_int_env(env, "DEPLOY_ROLLOUT_TIMEOUT_SECONDS", 120, 10, 900))


@dataclass(frozen=True)
class Target:
    namespace: str
    name: str
    container: str
    resource: str          # "memory" | "cpu" | "env" (a feature flag)
    old_value: str
    new_value: str
    env_name: str = ""     # the feature flag, when resource == "env"

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def field(self) -> str:
        """What the change sets, for notes and events."""
        return f"{self.env_name} flag" if self.resource == "env" else f"{self.resource} limit"


def target_from_proposal(target_ref: dict, proposed_values: dict) -> Target:
    """Builds the deployment target from a stored VALIDATED proposal, re-checking every value."""
    ref, values = target_ref or {}, proposed_values or {}
    field_path = values.get("field_path") or ref.get("field_path") or ""
    match, flag = _FIELD_PATH.match(field_path), _FLAG_PATH.match(field_path)
    if ref.get("kind") != "Deployment" or not (match or flag) or values.get("op") != "replace_scalar":
        raise DeployBlocked("TARGET_INVALID", "proposal does not describe a Deployment resource-limit or feature-flag change")
    target = Target(namespace=str(ref.get("namespace") or ""), name=str(ref.get("name") or ""),
                    container=str(ref.get("container") or ""), resource=match.group(1) if match else "env",
                    old_value=str(values.get("old_value") or ""), new_value=str(values.get("new_value") or ""),
                    env_name=flag.group(1) if flag else "")
    for label, value in (("namespace", target.namespace), ("name", target.name), ("container", target.container)):
        if not _K8S_NAME.match(value):
            raise DeployBlocked("TARGET_INVALID", f"{label} {value!r} is not a plain Kubernetes name")
    if flag:
        # A feature flag may only be turned off.
        if target.env_name not in FEATURE_FLAGS or (target.old_value, target.new_value) != ("true", "false"):
            raise DeployBlocked("TARGET_INVALID", f"only an allowlisted feature flag may change, from true to false")
        return target
    for label, value in (("old value", target.old_value), ("new value", target.new_value)):
        if not _QUANTITY.match(value):
            raise DeployBlocked("TARGET_INVALID", f"{label} {value!r} is not a plain quantity")
    return target


def check_allowed(cfg: DeployConfig, target: Target):
    if target.namespace in RESTRICTED_NAMESPACES:
        raise DeployBlocked("RESTRICTED_NAMESPACE", f"namespace {target.namespace} is protected")
    if target.key not in cfg.allowed_targets:
        raise DeployBlocked("TARGET_NOT_ALLOWLISTED", f"{target.key} is not in DEPLOY_ALLOWED_TARGETS")


# ─── kubectl ───

@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def subprocess_runner(argv: List[str], timeout: int) -> CommandResult:
    """Runs kubectl without a shell. Timeouts and a missing binary become non-zero results."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, shell=False)
        return CommandResult(done.returncode, done.stdout, done.stderr)
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout}s")
    except FileNotFoundError:
        return CommandResult(127, "", "kubectl not found (check KUBECTL_PATH)")
    except OSError as exc:
        return CommandResult(126, "", f"kubectl could not be started: {exc.__class__.__name__}")


class Kubectl:
    def __init__(self, cfg: DeployConfig, runner: Callable[[List[str], int], CommandResult] = subprocess_runner):
        self.cfg, self.runner, self.commands = cfg, runner, []

    def _run(self, namespace: str, args: List[str], timeout: int = 30) -> CommandResult:
        argv = [self.cfg.kubectl, "--context", self.cfg.context, "--namespace", namespace, *args]
        result = self.runner(argv, timeout)
        self.commands.append({"argv": argv[1:], "returncode": result.returncode,
                              "stderr": (result.stderr or "")[:MAX_OUTPUT]})
        return result

    def get_deployment(self, t: Target) -> dict:
        r = self._run(t.namespace, ["get", "deployment", t.name, "--output", "json"])
        if r.returncode != 0:
            code = "TARGET_NOT_FOUND" if "NotFound" in (r.stderr or "") else "CLUSTER_UNREACHABLE"
            raise DeployBlocked(code, (r.stderr or "kubectl get failed").strip()[:300])
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise DeployBlocked("CLUSTER_UNREACHABLE", "kubectl returned invalid JSON")

    def json_patch(self, t: Target, ops: list, dry_run: bool) -> CommandResult:
        args = ["patch", "deployment", t.name, "--type", "json", "--patch", json.dumps(ops),
                "--field-manager", "autofixops"]
        return self._run(t.namespace, args + (["--dry-run=server"] if dry_run else []))

    def rollout_status(self, t: Target) -> CommandResult:
        timeout = self.cfg.rollout_timeout_s
        return self._run(t.namespace, ["rollout", "status", f"deployment/{t.name}", f"--timeout={timeout}s"],
                         timeout=timeout + 15)

    def get_raw(self, namespace: str, path: str) -> CommandResult:
        """GET through the API server's service proxy (read-only), e.g. a health endpoint."""
        if not _SERVICE_PROXY.match(path):
            raise DeployBlocked("TARGET_INVALID", "only service-proxy paths may be read")
        return self._run(namespace, ["get", "--raw", path])

    def get_events(self, namespace: str) -> list:
        """Warning events in the namespace (read-only)."""
        r = self._run(namespace, ["get", "events", "--field-selector", "type=Warning", "--output", "json"])
        if r.returncode != 0:
            raise DeployBlocked("CLUSTER_UNREACHABLE", (r.stderr or "kubectl get events failed").strip()[:300])
        try:
            return json.loads(r.stdout).get("items", [])
        except (ValueError, AttributeError):
            raise DeployBlocked("CLUSTER_UNREACHABLE", "kubectl returned invalid JSON for events")

    def get_logs(self, namespace: str, pod: str, container: str, previous: bool, tail: int = 20) -> str:
        """Last log lines of one container (read-only). Empty string when none are available."""
        if not (_POD_NAME.match(pod) and _K8S_NAME.match(container)):
            raise DeployBlocked("TARGET_INVALID", "pod or container name is not a plain Kubernetes name")
        args = ["logs", pod, "--container", container, f"--tail={int(tail)}"]
        r = self._run(namespace, args + (["--previous"] if previous else []))
        return r.stdout if r.returncode == 0 else ""

    def get_pods(self, t: Target, selector: Dict[str, str]) -> list:
        if not selector or not all(re.match(r"^[A-Za-z0-9._/-]{1,253}$", f"{k}{v}") for k, v in selector.items()):
            raise DeployBlocked("TARGET_INVALID", "deployment selector is not a plain label set")
        label = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
        r = self._run(t.namespace, ["get", "pods", "--selector", label, "--output", "json"])
        if r.returncode != 0:
            raise DeployBlocked("CLUSTER_UNREACHABLE", (r.stderr or "kubectl get pods failed").strip()[:300])
        try:
            return json.loads(r.stdout).get("items", [])
        except (ValueError, AttributeError):
            raise DeployBlocked("CLUSTER_UNREACHABLE", "kubectl returned invalid JSON for pods")


# ─── Plan, apply, roll back ───

def container_index(live: dict, container: str) -> int:
    containers = (((live.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    for i, c in enumerate(containers):
        if c.get("name") == container:
            return i
    raise DeployBlocked("DRIFT_DETECTED", f"live deployment has no container {container!r}")


def live_limit(live: dict, index: int, resource: str) -> Optional[str]:
    c = live["spec"]["template"]["spec"]["containers"][index]
    return ((c.get("resources") or {}).get("limits") or {}).get(resource)


def env_index(live: dict, index: int, name: str) -> Optional[int]:
    """Position of the container's env entry `name` (None if it is not set)."""
    env = live["spec"]["template"]["spec"]["containers"][index].get("env") or []
    found = [i for i, e in enumerate(env) if isinstance(e, dict) and e.get("name") == name]
    return found[0] if len(found) == 1 else None


def live_flags(live: dict, index: int) -> Dict[str, Optional[str]]:
    """Literal values of the allowlisted feature flags in the live container (absent -> not listed)."""
    env = live["spec"]["template"]["spec"]["containers"][index].get("env") or []
    return {e["name"]: e.get("value") for e in env
            if isinstance(e, dict) and e.get("name") in FEATURE_FLAGS and "valueFrom" not in e}


def live_value(live: dict, index: int, t: Target) -> Optional[str]:
    """The live value of what the target changes: a resource limit or a feature flag."""
    if t.resource == "env":
        j = env_index(live, index, t.env_name)
        return None if j is None else live["spec"]["template"]["spec"]["containers"][index]["env"][j].get("value")
    return live_limit(live, index, t.resource)


def patch_ops(t: Target, index: int, expect: str, put: str, env_at: Optional[int] = None) -> list:
    base = f"/spec/template/spec/containers/{index}"
    if t.resource == "env":
        path = f"{base}/env/{env_at}"
        return [{"op": "test", "path": f"{base}/name", "value": t.container},
                {"op": "test", "path": f"{path}/name", "value": t.env_name},
                {"op": "test", "path": f"{path}/value", "value": expect},
                {"op": "replace", "path": f"{path}/value", "value": put}]
    return [{"op": "test", "path": f"{base}/name", "value": t.container},
            {"op": "test", "path": f"{base}/resources/limits/{t.resource}", "value": expect},
            {"op": "replace", "path": f"{base}/resources/limits/{t.resource}", "value": put}]


def snapshot(live: dict, index: int) -> dict:
    meta, spec = live.get("metadata") or {}, live.get("spec") or {}
    c = spec["template"]["spec"]["containers"][index]
    return {"generation": meta.get("generation"), "resource_version": meta.get("resourceVersion"),
            "replicas": spec.get("replicas"), "selector": (spec.get("selector") or {}).get("matchLabels"),
            "container": {"index": index, "name": c.get("name"), "image": c.get("image"),
                          "resources": c.get("resources"), "feature_flags": live_flags(live, index)}}


def apply_change(kube: Kubectl, t: Target) -> dict:
    """Dry run, apply, wait for the rollout. Returns the deployment record.

    Raises DeployBlocked when nothing was changed (unreachable cluster, drift, dry run refused)
    and DeployFailed(applied=True) when the patch was written but the rollout did not finish.
    """
    live = kube.get_deployment(t)
    index = container_index(live, t.container)
    current = live_value(live, index, t)
    if current != t.old_value:
        raise DeployBlocked("DRIFT_DETECTED", f"live {t.field} is {current!r}, proposal expects {t.old_value!r}")
    env_at = env_index(live, index, t.env_name) if t.resource == "env" else None
    record = {"previous": snapshot(live, index), "patch": patch_ops(t, index, t.old_value, t.new_value, env_at)}
    dry = kube.json_patch(t, record["patch"], dry_run=True)
    if dry.returncode != 0:
        raise DeployBlocked("DRY_RUN_REFUSED", (dry.stderr or "server-side dry run failed").strip()[:300])
    real = kube.json_patch(t, record["patch"], dry_run=False)
    if real.returncode != 0:
        # The JSON patch is atomic on the API server, but a client timeout or a dropped connection
        # can hide a patch that was applied. Re-read before calling it refused.
        error = (real.stderr or "kubectl patch failed").strip()[:300]
        after = live_value(kube.get_deployment(t), index, t)   # unreadable -> caller reconciles
        if after == t.new_value:
            record["patched"] = True
            raise DeployFailed("PATCH_RESULT_UNCONFIRMED", error, applied=True, record=record)
        raise DeployBlocked("PATCH_REFUSED", error)
    record["patched"] = True
    rollout = kube.rollout_status(t)
    record["rollout"] = {"returncode": rollout.returncode, "output": (rollout.stdout or rollout.stderr or "")[-MAX_OUTPUT:]}
    if rollout.returncode != 0:
        raise DeployFailed("ROLLOUT_FAILED", record["rollout"]["output"][:300] or "rollout did not complete",
                           applied=True, record=record)
    return record


def rollback_change(kube: Kubectl, t: Target, index: int) -> dict:
    """Puts the previous value back (only if the live value is still the one we set)."""
    env_at = None
    if t.resource == "env":
        try:
            env_at = env_index(kube.get_deployment(t), index, t.env_name)
        except DeployBlocked as exc:
            return {"patch": None, "ok": False, "error": f"{exc.code}: {exc.detail}"}
    ops = patch_ops(t, index, t.new_value, t.old_value, env_at)
    result = {"patch": ops}
    patched = kube.json_patch(t, ops, dry_run=False)
    result["patch_returncode"] = patched.returncode
    if patched.returncode != 0:
        result["ok"] = False
        result["error"] = (patched.stderr or "rollback patch refused").strip()[:300]
        return result
    rollout = kube.rollout_status(t)
    result["rollout"] = {"returncode": rollout.returncode, "output": (rollout.stdout or rollout.stderr or "")[-MAX_OUTPUT:]}
    result["ok"] = rollout.returncode == 0
    return result
