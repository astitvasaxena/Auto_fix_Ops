from fastapi import BackgroundTasks, FastAPI, Response
import asyncio
import os
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


class ContainerLimitCollector:
    """Exports the container's memory limit (cgroup v2/v1) so alert rules can compare usage with
    the limit that is actually in force, which changes when a remediation raises it."""

    def collect(self):
        for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            try:
                with open(path) as fh:
                    value = fh.read().strip()
            except OSError:
                continue
            if value.isdigit() and int(value) < 2 ** 60:   # "max" / huge = no limit: export nothing
                yield GaugeMetricFamily("target_app_memory_limit_bytes", "Container memory limit in bytes.",
                                        value=int(value))
            return


REGISTRY.register(ContainerLimitCollector())

# Global variables for chaos memory
data = []
cpu_stress_flag = False

# Crash-loop chaos (Kubernetes demo): memory the app needs at start-up, e.g. a cache it
# loads before serving. Set through the optional ConfigMap "target-app-chaos" (see
# engine/k8s_chaos.py). Above the container memory limit, every start is OOMKilled and
# Kubernetes reports CrashLoopBackOff, in every new pod, until the limit is raised.
STARTUP_MEMORY_MB = int(os.getenv("STARTUP_MEMORY_MB", "0") or 0)
startup_cache = b""


@app.on_event("startup")
def load_startup_cache():
    global startup_cache
    if STARTUP_MEMORY_MB > 0:
        print(f"[target-app] loading start-up cache: {STARTUP_MEMORY_MB} MB", flush=True)
        startup_cache = b"\x01" * (STARTUP_MEMORY_MB * 1024 * 1024)   # written, so really resident
        print("[target-app] start-up cache loaded", flush=True)

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
