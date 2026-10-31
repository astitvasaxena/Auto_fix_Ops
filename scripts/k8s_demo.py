"""
Disposable Kubernetes demo environment for Stage 4 (local clusters only).
==========================================================================
Sets up the target app in a LOCAL cluster (Docker Desktop Kubernetes by
default) so AutoFixOps can deploy an approved remediation and verify recovery.

  python scripts/k8s_demo.py setup     [--context docker-desktop]
  python scripts/k8s_demo.py status    [--context docker-desktop]
  python scripts/k8s_demo.py teardown  [--context docker-desktop]
  python scripts/k8s_demo.py target local|k8s   # which target Prometheus scrapes
  python scripts/k8s_demo.py chaos crashloop    # start-up needs > memory limit: OOMKilled loop
  python scripts/k8s_demo.py chaos reset        # remove the chaos requirement (manual recovery)

setup:    builds autofixops/target-app:latest, applies the namespace, the
          repository's deployment.yaml (memory limit 100Mi) and a NodePort
          service on 30081, waits for the rollout, then points Prometheus at
          host.docker.internal:30081.
teardown: deletes the autofixops namespace and points Prometheus back at the
          local process on port 8001.

Safety: only contexts that are obviously local (docker-desktop, kind-*,
minikube, rancher-desktop) are accepted. Commands run without a shell.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "kubernetes_integration" / "target_app"
TARGETS_FILE = REPO / "monitoring" / "targets" / "target_app.json"
NAMESPACE, DEPLOYMENT, IMAGE, NODE_PORT = "autofixops", "autofixops-target-app", "autofixops/target-app:latest", 30081
LOCAL_CONTEXTS = ("docker-desktop", "minikube", "rancher-desktop")


def run(argv, check=True, capture=False):
    print("$ " + " ".join(argv))
    result = subprocess.run(argv, text=True, capture_output=capture)
    if check and result.returncode != 0:
        sys.exit(f"command failed ({result.returncode}): {(result.stderr or '').strip()}")
    return result


def kubectl(context, *args, **kw):
    return run(["kubectl", "--context", context, *args], **kw)


def check_context(context):
    if not (context in LOCAL_CONTEXTS or context.startswith("kind-")):
        sys.exit(f"refusing context {context!r}: only local demo clusters are allowed ({', '.join(LOCAL_CONTEXTS)}, kind-*)")
    contexts = run(["kubectl", "config", "get-contexts", "-o", "name"], capture=True).stdout.split()
    if context not in contexts:
        sys.exit(f"kubectl context {context!r} not found (have: {contexts or 'none'}).\n"
                 "Enable it first, e.g. Docker Desktop > Settings > Kubernetes > Enable Kubernetes.")


def node_address(context="docker-desktop"):
    """Where Prometheus (a docker-compose container) reaches the NodePort.

    Docker Desktop's kind-based cluster does not publish NodePorts on the host, so Prometheus is
    attached to the `kind` Docker network and scrapes the node's internal IP. Otherwise the
    NodePort is on the host (host.docker.internal)."""
    nets = run(["docker", "network", "ls", "--format", "{{.Name}}"], capture=True).stdout.split()
    if "kind" not in nets:
        return f"host.docker.internal:{NODE_PORT}"
    inspect = run(["docker", "inspect", "autofixops-prometheus", "--format", "{{json .NetworkSettings.Networks}}"],
                  capture=True, check=False).stdout
    if '"kind"' not in inspect:
        run(["docker", "network", "connect", "kind", "autofixops-prometheus"])
    ip = kubectl(context, "get", "nodes", "-o", "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
                 capture=True).stdout.split()
    ipv4 = next((a for a in ip if a.count(".") == 3), None)   # the node may also report an IPv6 address
    if not ipv4:
        sys.exit(f"no IPv4 InternalIP for the node: {ip}")
    return f"{ipv4}:{NODE_PORT}"


def proxy_get(context, path):
    """GET the target through the API server's service proxy (works without a published NodePort)."""
    out = kubectl(context, "--namespace", NAMESPACE, "get", "--raw",
                  f"/api/v1/namespaces/{NAMESPACE}/services/target-app-nodeport:80/proxy{path}", check=False, capture=True)
    return out.stdout if out.returncode == 0 else None


def set_target(where, context="docker-desktop"):
    address = node_address(context) if where == "k8s" else "host.docker.internal:8001"
    entry = [{"targets": [address], "labels": {"environment": "kubernetes" if where == "k8s" else "local",
                                               "app": "target-app", "service": "target-app"}}]
    TARGETS_FILE.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    print(f"Prometheus target -> {address}")
    try:   # file_sd also refreshes on its own within 15s; a reload makes it immediate
        urllib.request.urlopen(urllib.request.Request("http://localhost:9090/-/reload", method="POST"), timeout=5)
    except Exception as exc:
        print(f"(Prometheus reload skipped: {exc.__class__.__name__}; it will pick the file up within 15s)")


def setup(context):
    check_context(context)
    run(["docker", "build", "-t", IMAGE, str(APP_DIR)])
    for name in ("namespace.yaml", "chaos-configmap.yaml", "deployment.yaml", "service-nodeport.yaml"):
        kubectl(context, "apply", "-f", str(APP_DIR / name))
    kubectl(context, "--namespace", NAMESPACE, "rollout", "status", f"deployment/{DEPLOYMENT}", "--timeout=180s")
    for _ in range(30):
        if proxy_get(context, "/health"):
            break
        time.sleep(2)
    else:
        sys.exit("target did not answer /health through the API server proxy")
    set_target("k8s", context)
    print("\nReady. Health: kube-service://autofixops/target-app-nodeport:80/health "
          "(stop the local process on :8001 to avoid confusion)")


def status(context):
    check_context(context)
    kubectl(context, "--namespace", NAMESPACE, "get", "deployment,pods,svc", "-o", "wide", check=False)
    out = kubectl(context, "--namespace", NAMESPACE, "get", "deployment", DEPLOYMENT, "-o",
                  "jsonpath={.spec.template.spec.containers[0].resources.limits}", check=False, capture=True)
    print(f"live limits: {out.stdout or out.stderr.strip()}")
    print(f"Prometheus target file: {TARGETS_FILE.read_text(encoding='utf-8').strip()}")


def teardown(context):
    check_context(context)
    kubectl(context, "delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true")
    set_target("local")


def _baseline_rss(context) -> float:
    """Current resident memory of the healthy target pod, read from its /metrics."""
    for line in (proxy_get(context, "/metrics") or "").splitlines():
        if line.startswith("process_resident_memory_bytes "):
            return float(line.split()[1])
    sys.exit("process_resident_memory_bytes not found on the target's /metrics (is the pod healthy?)")


def chaos(context, action):
    check_context(context)
    sys.path.insert(0, str(REPO))
    from engine import deployer as dep
    from engine import k8s_chaos

    cfg = dep.read_config({"DEPLOY_KUBE_CONTEXT": context, "DEPLOY_ALLOWED_TARGETS": f"{NAMESPACE}/{DEPLOYMENT}"})
    target = dep.Target(NAMESPACE, DEPLOYMENT, "target-app", "memory", "0", "0")
    try:
        if action == "crashloop":
            result = k8s_chaos.inject_crash_loop(cfg, target, _baseline_rss(context))
        else:
            result = k8s_chaos.reset(cfg, target)
    except dep.DeployBlocked as exc:
        sys.exit(f"{exc.code}: {exc.detail}")
    print(json.dumps(result, indent=2))
    print(f"watch: kubectl --context {context} -n {NAMESPACE} get pods -w")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "status", "teardown", "target", "chaos"])
    parser.add_argument("where", nargs="?", choices=["local", "k8s", "crashloop", "reset"])
    parser.add_argument("--context", default="docker-desktop")
    args = parser.parse_args()
    if args.command == "target":
        if args.where not in ("local", "k8s"):
            parser.error("target needs 'local' or 'k8s'")
        set_target(args.where, args.context)
    elif args.command == "chaos":
        if args.where not in ("crashloop", "reset"):
            parser.error("chaos needs 'crashloop' or 'reset'")
        chaos(args.context, args.where)
    else:
        {"setup": setup, "status": status, "teardown": teardown}[args.command](args.context)


if __name__ == "__main__":
    main()
