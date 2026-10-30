"""
Disposable Kubernetes demo environment for Stage 4 (local clusters only).
==========================================================================
Sets up the target app in a LOCAL cluster (Docker Desktop Kubernetes by
default) so AutoFixOps can deploy an approved remediation and verify recovery.

  python scripts/k8s_demo.py setup     [--context docker-desktop]
  python scripts/k8s_demo.py status    [--context docker-desktop]
  python scripts/k8s_demo.py teardown  [--context docker-desktop]
  python scripts/k8s_demo.py target local|k8s   # which target Prometheus scrapes

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


def set_target(where):
    address = f"host.docker.internal:{NODE_PORT}" if where == "k8s" else "host.docker.internal:8001"
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
    for name in ("namespace.yaml", "deployment.yaml", "service-nodeport.yaml"):
        kubectl(context, "apply", "-f", str(APP_DIR / name))
    kubectl(context, "--namespace", NAMESPACE, "rollout", "status", f"deployment/{DEPLOYMENT}", "--timeout=180s")
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://localhost:{NODE_PORT}/health", timeout=3) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(2)
    else:
        sys.exit(f"target did not answer on http://localhost:{NODE_PORT}/health")
    set_target("k8s")
    print(f"\nReady. Target app: http://localhost:{NODE_PORT}  (stop the local process on :8001 to avoid confusion)")


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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "status", "teardown", "target"])
    parser.add_argument("where", nargs="?", choices=["local", "k8s"])
    parser.add_argument("--context", default="docker-desktop")
    args = parser.parse_args()
    if args.command == "target":
        if not args.where:
            parser.error("target needs 'local' or 'k8s'")
        set_target(args.where)
    else:
        {"setup": setup, "status": status, "teardown": teardown}[args.command](args.context)


if __name__ == "__main__":
    main()
