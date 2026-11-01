"""
Browser-access launcher: mocked end-to-end scenarios (Windows only)
====================================================================
Runs the real scripts/start-target-access.ps1 under Windows PowerShell against a SIMULATED cluster:
fake_kubectl.py stands in for kubectl (with kubectl 1.36's measured port-forward behaviour) and small
local HTTP servers stand in for the target pods. Nothing touches Kubernetes, the AutoFixOps API or the
real port 30081: every scenario uses its own free local port.

Covers the Memory Leak, CPU Spike and Crash Loop outage-and-remediation sequences, plus a killed
port-forward, a port-forward that is alive but unusable, temporarily unreachable Kubernetes, no ready
pod, in-place container restarts, consecutive failed checks, chaos reset, an occupied port and a second
launcher. It is a runner script (like tests/evaluator/harness.py), not collected by pytest, because it
needs Windows PowerShell 5.1:

    python tests/browser_access/run_launcher_scenarios.py              # all scenarios
    python tests/browser_access/run_launcher_scenarios.py cpu_spike    # selected ones
"""
import datetime
import http.server
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(ROOT, "scripts", "start-target-access.ps1")
POWERSHELL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                          "powershell.exe")
LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] (.{23}) (.*)$")
_uids = itertools.count(1)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Backends:
    """One local HTTP server per simulated pod; its mode decides what /health and /ready answer."""

    def __init__(self):
        self.modes, self.servers = {}, []

    def start(self, pod, mode="healthy"):
        self.modes[pod] = mode
        modes = self.modes

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                m = modes[pod]
                if self.path == "/health":
                    code, body = (500, {"status": "error"}) if m == "error" else (200, {"status": "ok"})
                elif self.path == "/ready":
                    code, body = {"healthy": (200, {"status": "ready"}), "notready": (503, {"status": "overloaded"}),
                                  "error": (503, {"status": "error"})}[m]
                else:
                    code, body = 404, {"status": "not found"}
                body["pod"] = pod
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server.server_address[1]

    def close(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()


class Cluster:
    """The simulated cluster: pods, the chaos ConfigMap and API reachability, written for fake_kubectl."""

    def __init__(self, workdir):
        self.path = os.path.join(workdir, "state.json")
        self.backends = Backends()
        self.state = {"contexts": ["docker-desktop"], "api_down": False, "stale_pids": [], "pods": [],
                      "service_name": "target-app-nodeport",
                      "service": {"spec": {"selector": {"app": "target-app", "system": "autofixops"},
                                           "ports": [{"port": 80, "targetPort": 8000, "nodePort": 30081}]}},
                      "configmap": {"LEAK_MB_PER_MIN": "0", "CPU_DEMAND_MILLICORES": "0", "STARTUP_MEMORY_MB": "0",
                                    "CHAOS_EXPIRES_AT": "0"}}
        self.write()

    def write(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)
        for _ in range(200):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:   # fake_kubectl is reading it right now
                time.sleep(0.01)
        raise RuntimeError("could not update the simulated cluster")

    def add_pod(self, name, phase="Running", container="running", ready=True, mode="healthy", **extra):
        created = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=next(_uids))
        pod = {"name": name, "uid": f"00000000-0000-0000-0000-{next(_uids):012d}", "phase": phase,
               "container": container, "ready": ready, "restarts": 0, "reason": None, "last_exit": None,
               "terminating": False, "created": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "backend_port": self.backends.start(name, mode)}
        pod.update(extra)
        self.state["pods"].append(pod)
        self.write()

    def set_pod(self, name, mode=None, **fields):
        pod = next(p for p in self.state["pods"] if p["name"] == name)
        pod.update(fields)
        if mode:
            self.backends.modes[name] = mode
        self.write()

    def delete_pod(self, name):
        self.state["pods"] = [p for p in self.state["pods"] if p["name"] != name]
        self.write()

    def set_chaos(self, leak=0, cpu=0, startup=0, minutes=15):
        expires = int(time.time() + minutes * 60) if (leak or cpu or startup) else 0
        self.state["configmap"] = {"LEAK_MB_PER_MIN": str(leak), "CPU_DEMAND_MILLICORES": str(cpu),
                                   "STARTUP_MEMORY_MB": str(startup), "CHAOS_EXPIRES_AT": str(expires)}
        self.write()

    def set_api(self, up):
        self.state["api_down"] = not up
        self.write()

    def forwards(self):
        """Port-forwards started so far, oldest first: [(pid, pod)]."""
        try:
            with open(self.path + ".portforwards", encoding="utf-8") as fh:
                return [(int(pid), pod) for pid, pod in (line.split() for line in fh if line.strip())]
        except FileNotFoundError:
            return []

    def live_forwards(self):
        return [(pid, pod) for pid, pod in self.forwards() if pid_alive(pid)]

    def make_current_forward_stale(self):
        self.state["stale_pids"] = [pid for pid, _ in self.live_forwards()]
        self.write()


def pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def http_get(port, path):
    """(status, body) through the tunnel, or (None, error) when nothing answered."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=4) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (OSError, ValueError) as e:
        return None, type(e).__name__


class Launcher:
    def __init__(self, workdir, cluster, port):
        self.port, self.out = port, os.path.join(workdir, f"launcher-{port}.log")
        fake_dir = os.path.join(workdir, "bin")
        os.makedirs(fake_dir, exist_ok=True)
        with open(os.path.join(fake_dir, "kubectl.cmd"), "w", encoding="ascii") as fh:
            fh.write(f'@"{sys.executable}" "{os.path.join(HERE, "fake_kubectl.py")}" %*\r\n')
        self.env = dict(os.environ, FAKE_KUBE_STATE=cluster.path, PATH=fake_dir + os.pathsep + os.environ["PATH"])
        self.proc = None

    def args(self, *extra):
        return [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT, "-LocalPort", str(self.port),
                "-PollSeconds", "1", "-ProbeSeconds", "1", *extra]

    def start(self):
        self.fh = open(self.out, "w", encoding="utf-8")
        self.proc = subprocess.Popen(self.args(), stdout=self.fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=self.env)
        return self

    def run_once(self, *extra, timeout=60):
        """Runs a separate launcher to completion: (exit code, output)."""
        r = subprocess.run(self.args(*extra), capture_output=True, text=True, stdin=subprocess.DEVNULL, env=self.env,
                           timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def events(self):
        with open(self.out, encoding="utf-8", errors="replace") as fh:
            return [(m.group(1).strip(), m.group(2)) for m in (LINE.match(l.rstrip("\n")) for l in fh) if m]

    def mark(self):
        return len(self.events())

    def wait_for(self, label, text="", since=0, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for i, (lab, msg) in enumerate(self.events()[since:], start=since):
                if lab == label and text in msg:
                    return i + 1
            assert self.proc.poll() is None, f"launcher exited ({self.proc.returncode}) while waiting for {label}\n" + self.dump()
            time.sleep(0.3)
        raise AssertionError(f"no '{label}' containing '{text}' within {timeout}s\n" + self.dump())

    def count(self, label, since=0):
        return sum(1 for lab, _ in self.events()[since:] if lab == label)

    def dump(self):
        with open(self.out, encoding="utf-8", errors="replace") as fh:
            return "---- launcher output ----\n" + fh.read()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
            self.proc.wait(10)
            # killed, so its own clean-up did not run
            shutil.rmtree(os.path.join(tempfile.gettempdir(), f"autofixops-target-access-{self.proc.pid}"), ignore_errors=True)
        self.fh.close()


def scenario(fn):
    """Fresh simulated cluster with one healthy pod and a running launcher that reports it healthy."""
    def run():
        workdir = tempfile.mkdtemp(prefix="afo-launcher-")
        cluster = Cluster(workdir)
        cluster.add_pod("target-a")
        launcher = Launcher(workdir, cluster, free_port()).start()
        try:
            launcher.wait_for("APPLICATION HEALTHY", "pod target-a")
            fn(cluster, launcher)
            assert launcher.proc.poll() is None, "the launcher must still be running\n" + launcher.dump()
        finally:
            launcher.stop()
            cluster.backends.close()
            left = [pid for pid, _ in cluster.live_forwards()]
            for pid in left:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            shutil.rmtree(workdir, ignore_errors=True)
            assert not left, f"port-forward processes left behind: {left}"
    run.__name__ = fn.__name__
    SCENARIOS[fn.__name__] = run
    return run


SCENARIOS = {}


def outage_churn(cluster, launcher, seconds, refreshes=0):
    """Waits through an outage, optionally refreshing the browser, and returns (port-forwards started,
    RECONNECTING lines printed) during it."""
    before, mark = len(cluster.forwards()), launcher.mark()
    for i in range(refreshes):
        assert http_get(launcher.port, "/health")[0] is None, "the browser must not get an answer during the outage"
        time.sleep(seconds / max(refreshes, 1))
    time.sleep(seconds if not refreshes else 0)
    return len(cluster.forwards()) - before, launcher.count("RECONNECTING", mark)


@scenario
def healthy(cluster, launcher):
    assert http_get(launcher.port, "/health") == (200, {"status": "ok", "pod": "target-a"})
    assert http_get(launcher.port, "/ready")[0] == 200
    assert launcher.count("BROWSER ACCESS RESTORED") == 0


@scenario
def port_forward_killed(cluster, launcher):
    pid = cluster.live_forwards()[-1][0]
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    m = launcher.wait_for("RECONNECTING", "stopped")
    launcher.wait_for("BROWSER ACCESS RESTORED", since=m)
    assert http_get(launcher.port, "/health")[0] == 200


@scenario
def port_forward_alive_but_unusable(cluster, launcher):
    stale = cluster.live_forwards()[-1][0]
    cluster.make_current_forward_stale()
    m = launcher.wait_for("RECONNECTING", "port-forward unusable")
    launcher.wait_for("BROWSER ACCESS RESTORED", since=m)
    assert not pid_alive(stale), "the unusable port-forward must be stopped"
    assert http_get(launcher.port, "/health")[0] == 200


@scenario
def memory_leak(cluster, launcher):
    # Injection: the ConfigMap gets the leak and the running pod is deleted; the replacement starts.
    cluster.set_chaos(leak=40)
    cluster.delete_pod("target-a")
    cluster.add_pod("target-b", phase="Pending", container="waiting", ready=False, reason="ContainerCreating")
    m = launcher.wait_for("POD REPLACED", "target-a")
    m = launcher.wait_for("WAITING FOR READY POD", since=m)
    cluster.set_pod("target-b", phase="Running", container="running", ready=True, reason=None)
    m = launcher.wait_for("BROWSER ACCESS RESTORED", "Chaos is still configured (Memory Leak (40 MB/min)", since=m)
    # The leak grows until the container is OOM-killed and crash-loops, in the same pod.
    cluster.set_pod("target-b", container="waiting", reason="CrashLoopBackOff", ready=False, restarts=1, last_exit="OOMKilled")
    m = launcher.wait_for("CHAOS OUTAGE DETECTED", "Memory Leak", since=m)
    m = launcher.wait_for("APPLICATION DOWN", "CrashLoopBackOff", since=m - 1)
    started, reconnecting = outage_churn(cluster, launcher, 10, refreshes=3)
    assert reconnecting == 0, "an expected outage must not be reported as reconnects\n" + launcher.dump()
    assert started <= 4, f"{started} port-forwards in 10 s of outage: reconnect loop\n" + launcher.dump()
    assert launcher.wait_for("INFO", "reopened automatically while the outage lasts", since=m)
    # Kubernetes restarts the container in place; it serves again until the next OOM kill.
    cluster.set_pod("target-b", container="running", reason=None, ready=True)
    m = launcher.wait_for("BROWSER ACCESS RESTORED", "pod target-b", since=m)
    assert launcher.count("POD REPLACED", m - 5) == 0
    cluster.set_pod("target-b", container="waiting", reason="CrashLoopBackOff", ready=False, restarts=2)
    m = launcher.wait_for("APPLICATION DOWN", since=m)
    # Remediation (RESPONSE_CACHE_ENABLED=false) rolls out a new pod; the old one is removed after it is ready.
    cluster.add_pod("target-c")
    m = launcher.wait_for("POD REPLACED", "ready pod target-c", since=m)
    m = launcher.wait_for("BROWSER ACCESS RESTORED", "pod target-c", since=m)
    cluster.set_pod("target-b", terminating=True)
    time.sleep(2)
    cluster.delete_pod("target-b")
    time.sleep(3)
    assert launcher.count("POD REPLACED", m) == 0, "the old pod's removal must not disturb the tunnel on the new pod"
    assert http_get(launcher.port, "/health") == (200, {"status": "ok", "pod": "target-c"})
    assert http_get(launcher.port, "/ready")[0] == 200


@scenario
def cpu_spike(cluster, launcher):
    cluster.set_chaos(cpu=650)
    cluster.delete_pod("target-a")
    cluster.add_pod("target-b")
    m = launcher.wait_for("BROWSER ACCESS RESTORED", "pod target-b")
    # The backlog grows: /ready fails, the pod stays Running but NotReady; nothing crashes.
    cluster.set_pod("target-b", mode="notready", ready=False)
    m = launcher.wait_for("CHAOS OUTAGE DETECTED", "CPU Spike (650m CPU demand)", since=m)
    m = launcher.wait_for("APPLICATION NOT READY", "/ready HTTP 503 (overloaded)", since=m - 1)
    before = len(cluster.forwards())
    time.sleep(8)
    assert len(cluster.forwards()) == before, "a Running but NotReady pod must keep its tunnel\n" + launcher.dump()
    assert http_get(launcher.port, "/health")[0] == 200 and http_get(launcher.port, "/ready")[0] == 503
    # Remediation (larger CPU limit): the new pod is ready while the old one is still NotReady.
    cluster.add_pod("target-c")
    m = launcher.wait_for("POD REPLACED", "ready pod target-c", since=m)
    m = launcher.wait_for("BROWSER ACCESS RESTORED", "pod target-c", since=m)
    cluster.set_pod("target-b", terminating=True)
    time.sleep(2)
    cluster.delete_pod("target-b")
    time.sleep(3)
    assert launcher.count("POD REPLACED", m) == 0
    assert http_get(launcher.port, "/ready") == (200, {"status": "ready", "pod": "target-c"})


@scenario
def crash_loop(cluster, launcher):
    cluster.set_chaos(startup=200)
    cluster.delete_pod("target-a")
    cluster.add_pod("target-b", container="waiting", reason="ContainerCreating", ready=False)
    m = launcher.wait_for("POD REPLACED", "target-a")
    # The container dies at start-up over and over; there is never a ready pod.
    for restarts in range(1, 5):
        cluster.set_pod("target-b", container="waiting", reason="CrashLoopBackOff", ready=False, restarts=restarts,
                        last_exit="OOMKilled")
        if restarts == 1:
            m = launcher.wait_for("CHAOS OUTAGE DETECTED", "Crash Loop (200 MB at start-up)", since=m)
        started, reconnecting = outage_churn(cluster, launcher, 4, refreshes=1)
        assert reconnecting == 0 and started <= 3, (started, reconnecting, launcher.dump())
        cluster.set_pod("target-b", container="running", reason=None, listening=False)   # starts, dies before serving
        time.sleep(1)
    assert launcher.count("BROWSER ACCESS RESTORED", m) == 0, "no restore may be claimed during the crash loop"
    # Remediation (larger memory limit): no pod at all for a while, then the new pod starts and becomes ready.
    cluster.delete_pod("target-b")
    cluster.add_pod("target-c", phase="Pending", container="waiting", ready=False, reason="ContainerCreating")
    m = launcher.wait_for("WAITING FOR READY POD", since=m)
    time.sleep(6)
    cluster.set_pod("target-c", phase="Running", container="running", ready=True, reason=None)
    launcher.wait_for("BROWSER ACCESS RESTORED", "pod target-c", since=m)
    assert http_get(launcher.port, "/health") == (200, {"status": "ok", "pod": "target-c"})


@scenario
def container_restart_in_place(cluster, launcher):
    cluster.set_pod("target-a", container="terminated", reason="Error", ready=False, restarts=1, last_exit="Error")
    m = launcher.wait_for("APPLICATION DOWN", "terminated")
    assert launcher.count("CHAOS OUTAGE DETECTED", m - 1) == 0, "no chaos is configured"
    cluster.set_pod("target-a", container="running", reason=None, ready=True)
    m = launcher.wait_for("BROWSER ACCESS RESTORED", "pod target-a", since=m)
    assert launcher.count("POD REPLACED") == 0
    assert http_get(launcher.port, "/health")[0] == 200


@scenario
def consecutive_failed_checks(cluster, launcher):
    before = len(cluster.forwards())
    cluster.set_pod("target-a", mode="error", ready=False)
    m = launcher.wait_for("APPLICATION DOWN", "/health HTTP 500")
    time.sleep(6)   # several failed checks in a row
    assert launcher.count("RECONNECTING", m) == 0 and len(cluster.forwards()) == before, \
        "HTTP errors prove the tunnel works; it must not be replaced\n" + launcher.dump()
    cluster.set_pod("target-a", mode="healthy", ready=True)
    launcher.wait_for("BROWSER ACCESS RESTORED", since=m)


@scenario
def kubernetes_temporarily_unreachable(cluster, launcher):
    cluster.set_api(up=False)
    m = launcher.wait_for("WARN", "The current tunnel is kept")
    assert http_get(launcher.port, "/health")[0] == 200, "the tunnel keeps working while the API is down"
    pid = cluster.live_forwards()[-1][0]
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    m = launcher.wait_for("WAITING FOR KUBERNETES", since=m)
    time.sleep(3)
    cluster.set_api(up=True)
    launcher.wait_for("INFO", "reachable again", since=m)
    launcher.wait_for("BROWSER ACCESS RESTORED", since=m)
    assert http_get(launcher.port, "/health")[0] == 200


@scenario
def chaos_reset_recreates_pod(cluster, launcher):
    cluster.set_chaos(leak=40)
    cluster.set_chaos()   # reset clears every fault and restarts the pods
    cluster.set_pod("target-a", terminating=True)
    cluster.add_pod("target-b")
    m = launcher.wait_for("POD REPLACED", "target-a")
    m = launcher.wait_for("BROWSER ACCESS RESTORED", "pod target-b", since=m)
    assert "Chaos is still configured" not in launcher.events()[m - 1][1]
    cluster.delete_pod("target-a")
    assert http_get(launcher.port, "/health")[1]["pod"] == "target-b"


@scenario
def second_launcher_refused(cluster, launcher):
    code, out = launcher.run_once()
    assert code == 2 and "Another START_TARGET_APP launcher" in out, out
    assert http_get(launcher.port, "/health")[0] == 200, "the first launcher keeps working"


def port_occupied():
    workdir = tempfile.mkdtemp(prefix="afo-launcher-")
    cluster = Cluster(workdir)
    cluster.add_pod("target-a")
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        launcher = Launcher(workdir, cluster, blocker.getsockname()[1])
        code, out = launcher.run_once()
    cluster.backends.close()
    shutil.rmtree(workdir, ignore_errors=True)
    assert code == 2 and "is already in use" in out and "python" in out.lower(), out


def check_only():
    workdir = tempfile.mkdtemp(prefix="afo-launcher-")
    cluster = Cluster(workdir)
    launcher = Launcher(workdir, cluster, free_port())
    code, out = launcher.run_once("-CheckOnly")
    assert code == 0 and "all checks passed" in out, out
    cluster.state["service_name"] = "other"
    cluster.write()
    code, out = launcher.run_once()
    assert code == 1 and "was not found in namespace" in out, out
    cluster.set_api(up=False)
    code, out = launcher.run_once()
    assert code == 1 and "is not reachable" in out, out
    cluster.backends.close()
    shutil.rmtree(workdir, ignore_errors=True)


SCENARIOS["port_occupied"] = port_occupied
SCENARIOS["check_only"] = check_only


def main(names):
    if os.name != "nt":
        print("SKIPPED: needs Windows PowerShell 5.1")
        return 0
    failed = []
    for name in names or list(SCENARIOS):
        t0 = time.time()
        try:
            SCENARIOS[name]()
            print(f"PASS  {name} ({time.time() - t0:.0f}s)", flush=True)
        except Exception as exc:   # noqa: BLE001 - report every scenario
            failed.append(name)
            print(f"FAIL  {name} ({time.time() - t0:.0f}s)\n{exc}", flush=True)
    print(f"\n{len(names or SCENARIOS) - len(failed)} passed, {len(failed)} failed" + (f": {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
