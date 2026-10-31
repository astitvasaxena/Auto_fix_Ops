import os
from celery import Celery

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "autofixops_workers",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["workers.tasks"]
)

# Optional config for celery durability
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_retries=3,
    # Acknowledge a task only after it finished, so a task running when the worker is killed is
    # delivered again (every pipeline task re-checks the incident/run state and claims its step,
    # so a second delivery is harmless). Redis restores unacknowledged tasks after the visibility
    # timeout, which must exceed the longest task and the longest countdown (PR polling <= 900 s).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 1800},
)
