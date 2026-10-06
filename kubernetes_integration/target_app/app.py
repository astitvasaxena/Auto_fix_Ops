from fastapi import BackgroundTasks, FastAPI, Response
import asyncio
import os
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

app = FastAPI(title="AutoFixOps Target App")

# Global variables for chaos memory
data = []
cpu_stress_flag = False

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
