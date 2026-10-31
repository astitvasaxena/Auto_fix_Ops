from fastapi import BackgroundTasks, FastAPI, Response
import asyncio
import os
import threading
import time
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

app = FastAPI(title="AutoFixOps Target App")


class PortableProcessCollector:
    """prometheus_client's built-in process collector reads Linux /proc only, so on
    Windows/macOS /metrics had no memory or CPU series and the memory/crash alerts
    could never fire. This exports the same standard metric names via psutil."""

    def collect(self):
        import psutil

        proc = psutil.Process()
        mem, cpu = proc.memory_info(), proc.cpu_times()
        yield GaugeMetricFamily("process_resident_memory_bytes", "Resident memory size in bytes.", value=mem.rss)
        yield GaugeMetricFamily("process_virtual_memory_bytes", "Virtual memory size in bytes.", value=mem.vms)
        yield CounterMetricFamily("process_cpu_seconds_total", "Total user and system CPU time spent in seconds.",
                                  value=cpu.user + cpu.system)
        yield GaugeMetricFamily("process_start_time_seconds", "Start time of the process since unix epoch in seconds.",
                                value=proc.create_time())


if not os.path.exists("/proc/self/stat"):   # not Linux: the built-in collector exports nothing
    REGISTRY.register(PortableProcessCollector())


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def cpu_limit_cores(cpu_max):
    """cgroup v2 cpu.max ("50000 100000" -> 0.5 cores; "max 100000" -> None)."""
    parts = (cpu_max or "").split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) > 0:
        return int(parts[0]) / int(parts[1])
    return None


class ContainerLimitCollector:
    """Exports the container's memory and CPU limits (cgroup v2, memory also v1) so alert rules
    compare usage with the limits actually in force, which change when a remediation raises them."""

    def collect(self):
        for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            value = _read(path)
            if value is None:
                continue
            if value.isdigit() and int(value) < 2 ** 60:   # "max" / huge = no limit: export nothing
                yield GaugeMetricFamily("target_app_memory_limit_bytes", "Container memory limit in bytes.",
                                        value=int(value))
            break
        cores = cpu_limit_cores(_read("/sys/fs/cgroup/cpu.max"))
        if cores:
            yield GaugeMetricFamily("target_app_cpu_limit_cores", "Container CPU limit in cores.", value=cores)


REGISTRY.register(ContainerLimitCollector())

# Global variables for chaos memory
data = []
cpu_stress_flag = False

# ─── Kubernetes chaos (set through the optional ConfigMap "target-app-chaos", engine/k8s_chaos.py) ───
# Read once at start-up, so a fault persists in every new container and pod until the ConfigMap is
# reset. Every fault is ignored unless CHAOS_EXPIRES_AT (unix time) is set and still in the future,
# and the background ones stop at that time: a forgotten experiment ends by itself.
#   STARTUP_MEMORY_MB      memory needed at start-up (a cache loaded before serving). Above the
#                          memory limit every start is OOMKilled -> CrashLoopBackOff.
#   LEAK_MB_PER_MIN        memory that keeps leaking while the app runs -> OOMKilled, again after
#                          every restart.
#   CPU_DEMAND_MILLICORES  CPU work the app needs continuously (one thread, at most 1000m). Above
#                          the CPU limit the container is pinned at the limit and throttled.
def _int_env(name):
    try:
        return max(0, int(os.getenv(name, "0") or 0))
    except ValueError:
        return 0


def chaos_settings(now=None):
    now = time.time() if now is None else now
    try:
        expires = float(os.getenv("CHAOS_EXPIRES_AT", "0") or 0)
    except ValueError:
        expires = 0.0
    active = expires > now
    return {"expires_at": expires, "active": active,
            "startup_memory_mb": _int_env("STARTUP_MEMORY_MB") if active else 0,
            "leak_mb_per_min": _int_env("LEAK_MB_PER_MIN") if active else 0,
            "cpu_demand_millicores": min(1000, _int_env("CPU_DEMAND_MILLICORES")) if active else 0}


CHAOS = chaos_settings()
STARTUP_MEMORY_MB = CHAOS["startup_memory_mb"]
startup_cache = b""
leaked = []


def _leak_forever(mb_per_min, expires_at):
    pause = 60.0 / mb_per_min
    while time.time() < expires_at:
        leaked.append(b"\x01" * (1024 * 1024))   # written, so really resident
        time.sleep(pause)


def _burn_cpu(millicores, expires_at, period=0.1):
    busy = period * millicores / 1000.0
    while time.time() < expires_at:
        start = time.perf_counter()
        while time.perf_counter() - start < busy:
            pass
        time.sleep(max(0.0, period - busy))


@app.on_event("startup")
def load_startup_cache():
    global startup_cache
    if STARTUP_MEMORY_MB > 0:
        print(f"[target-app] loading start-up cache: {STARTUP_MEMORY_MB} MB", flush=True)
        startup_cache = b"\x01" * (STARTUP_MEMORY_MB * 1024 * 1024)   # written, so really resident
        print("[target-app] start-up cache loaded", flush=True)
    if CHAOS["leak_mb_per_min"] > 0:
        print(f"[target-app] chaos: leaking {CHAOS['leak_mb_per_min']} MB/min", flush=True)
        threading.Thread(target=_leak_forever, args=(CHAOS["leak_mb_per_min"], CHAOS["expires_at"]),
                         daemon=True).start()
    if CHAOS["cpu_demand_millicores"] > 0:
        print(f"[target-app] chaos: CPU demand {CHAOS['cpu_demand_millicores']}m", flush=True)
        threading.Thread(target=_burn_cpu, args=(CHAOS["cpu_demand_millicores"], CHAOS["expires_at"]),
                         daemon=True).start()

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """Exposes the standard process metrics Prometheus scrapes."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/leak")
def leak():
    """
    Simulates a memory leak by continuously appending data to a global list.
    Each call adds ~10MB of string data.
    """
    global data
    data.append("x" * 10**7)
    return {"status": "leaking", "current_mb_leaked": len(data) * 10}

@app.get("/cpu")
def cpu():
    """
    Simulates a CPU spike by pinning a worker thread in an infinite loop.
    This will also block the health endpoint from responding if run synchronously,
    intentionally failing the liveness probe.
    """
    global cpu_stress_flag
    cpu_stress_flag = True
    while cpu_stress_flag:
        pass
    return {"status": "cpu_spiking"}


async def _terminate_after_response():
    """Lets the chaos caller receive its acknowledgement before process exit."""
    await asyncio.sleep(0.1)
    os._exit(1)


@app.get("/_chaos/crash", status_code=202)
async def crash(background_tasks: BackgroundTasks):
    """Existing crash-loop chaos hook used by the dashboard and API wrapper."""
    background_tasks.add_task(_terminate_after_response)
    return {"status": "crashing"}

@app.get("/recover")
def recover():
    """
    Simulates successful recovery/state neutralization.
    """
    global data
    global cpu_stress_flag
    data = []
    cpu_stress_flag = False
    return {"status": "recovered"}
