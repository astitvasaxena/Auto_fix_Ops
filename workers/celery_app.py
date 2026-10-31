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
)
