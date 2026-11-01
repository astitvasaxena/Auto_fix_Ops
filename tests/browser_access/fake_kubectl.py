"""Test double for kubectl, used by run_launcher_scenarios.py to drive scripts/start-target-access.ps1
without a cluster.

It reads the simulated cluster from the JSON file named by FAKE_KUBE_STATE (written by the runner) and
answers the few commands the launcher uses: config get-contexts, get service/configmap/pods and
port-forward. port-forward behaves like kubectl 1.36 against Docker Desktop, as measured:

* the first connection the pod refuses (container not running, or not listening yet) ends the port-forward with
  "connection refused ... error: lost connection to pod" and exit code 1;
* a connection after the pod is gone ends it with "failed to find sandbox ... lost connection to pod";
* a process listed in "stale_pids" stays alive but drops every connection (alive but unusable).

Each port-forward appends "<pid> <pod>" to FAKE_KUBE_STATE + ".portforwards" so the runner can count
and kill them.
"""
import json
import os
import socket
import sys
import threading
import time

STATE = os.environ["FAKE_KUBE_STATE"]
UNREACHABLE = ("Unable to connect to the server: dial tcp 127.0.0.1:6443: connectex: No connection could be made "
               "because the target machine actively refused it.")


def load():
    for _ in range(200):
        try:
            with open(STATE, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            time.sleep(0.01)
    raise SystemExit("fake kubectl: cannot read " + STATE)


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    sys.exit(code)


def find(state, name):
    return next((p for p in state["pods"] if p["name"] == name), None)


def pod_json(p):
    status = {"name": "target-app", "restartCount": p["restarts"], "ready": p["ready"]}
    if p["container"] == "running":
        status["state"] = {"running": {"startedAt": p["created"]}}
    elif p["container"] == "waiting":
        status["state"] = {"waiting": {"reason": p.get("reason") or "ContainerCreating"}}
    else:
        status["state"] = {"terminated": {"reason": p.get("reason") or "Error"}}
    if p.get("last_exit"):
        status["lastState"] = {"terminated": {"reason": p["last_exit"]}}
    meta = {"name": p["name"], "uid": p["uid"], "creationTimestamp": p["created"]}
    if p.get("terminating"):
        meta["deletionTimestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"metadata": meta,
            "spec": {"containers": [{"name": "target-app", "ports": [{"containerPort": 8000}]}]},
            "status": {"phase": p["phase"], "containerStatuses": [status],
                       "conditions": [{"type": "Ready", "status": "True" if p["ready"] else "False"}]}}


def pipe(conn, backend_port):
    try:
        upstream = socket.create_connection(("127.0.0.1", backend_port), timeout=10)
    except OSError:
        conn.close()
        return

    def copy(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    t = threading.Thread(target=copy, args=(upstream, conn), daemon=True)
    t.start()
    copy(conn, upstream)
    t.join(10)
    conn.close()
    upstream.close()


def die(message):
    sys.stderr.write(message + "\nerror: lost connection to pod\n")
    sys.stderr.flush()
    os._exit(1)


def port_forward(args):
    address = args[args.index("--address") + 1]
    name = next(a for a in args if a.startswith("pod/"))[4:]
    local_port, pod_port = (int(x) for x in args[-1].split(":"))
    state = load()
    if state.get("api_down"):
        fail(UNREACHABLE)
    pod = find(state, name)
    if not pod:
        fail(f'Error from server (NotFound): pods "{name}" not found')
    if pod["phase"] != "Running":
        fail(f"error: unable to forward port because pod is not running. Current status={pod['phase']}")
    server = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        server.bind((address, local_port))
    except OSError as exc:
        fail(f"Unable to listen on port {local_port}: {exc}\nerror: unable to listen on any of the requested ports")
    server.listen(16)
    with open(STATE + ".portforwards", "a", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()} {name}\n")
    print(f"Forwarding from {address}:{local_port} -> {pod_port}", flush=True)
    sandbox = pod["uid"].replace("-", "")
    while True:
        conn, _ = server.accept()
        print(f"Handling connection for {local_port}", flush=True)
        state = load()
        pod = find(state, name)
        if os.getpid() in state.get("stale_pids", []):
            conn.close()
            continue
        if not pod:   # a terminating pod still serves until it is gone, as in Kubernetes
            conn.close()
            die(f'E1101 portforward.go:522] "Unhandled Error" err="an error occurred forwarding {local_port} -> {pod_port}: '
                f'error forwarding port {pod_port} to pod {sandbox}, uid : failed to find sandbox \\"{sandbox}\\" in store: not found"')
        if pod["container"] != "running" or not pod.get("listening", True):   # crashed, or not listening yet
            conn.close()
            die(f'E1101 portforward.go:522] "Unhandled Error" err="an error occurred forwarding {local_port} -> {pod_port}: '
                f'error forwarding port {pod_port} to pod {sandbox}, uid : failed to execute portforward in network namespace: '
                f'failed to connect to localhost:{pod_port} inside namespace \\"{sandbox}\\", IPv4: dial tcp4 127.0.0.1:{pod_port}: '
                f'connect: connection refused"')
        threading.Thread(target=pipe, args=(conn, pod["backend_port"]), daemon=True).start()


def main(argv):
    args = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--context", "--namespace", "-n"):
            i += 2
            continue
        if not argv[i].startswith("--request-timeout"):
            args.append(argv[i])
        i += 1
    state = load()
    if args[:2] == ["config", "get-contexts"]:
        print("\n".join(state["contexts"]))
        return
    if args[0] == "port-forward":
        port_forward(args)
        return
    if state.get("api_down"):
        fail(UNREACHABLE)
    if args[0] == "get" and args[1] == "service":
        if args[2] != state["service_name"]:
            fail(f'Error from server (NotFound): services "{args[2]}" not found')
        print(json.dumps(state["service"]))
    elif args[0] == "get" and args[1] == "configmap":
        if state.get("configmap") is None:
            fail(f'Error from server (NotFound): configmaps "{args[2]}" not found')
        print(json.dumps({"data": state["configmap"]}))
    elif args[0] == "get" and args[1] == "pods":
        print(json.dumps({"items": [pod_json(p) for p in state["pods"]]}))
    else:
        fail("fake kubectl: unsupported command: " + " ".join(argv))


if __name__ == "__main__":
    main(sys.argv[1:])
