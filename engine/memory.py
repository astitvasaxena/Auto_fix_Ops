"""
Qdrant RAG memory layer.

Process-singleton client (one per Python process) to avoid the
"Storage folder is already accessed by another instance" error
from local Qdrant when multiple imports happen.

Storage location is configurable (default unchanged: in-memory):
  QDRANT_URL   — Qdrant server, e.g. http://localhost:6333 (multi-process safe)
  QDRANT_PATH  — local on-disk store, e.g. ./qdrant_data (one process at a time)
  neither      — in-memory, wiped when the process exits

Collections:
  incident_resolutions — verified fixes (written by VerificationEngine)
  incident_failures    — failed fixes (written by VerificationEngine)
  operational_runbooks — operational guidance curated from official Kubernetes /
                         Prometheus documentation (written only by
                         scripts/seed_rag_runbooks.py); never real incidents

All retrieval methods degrade gracefully — if anything goes wrong,
we return an empty list rather than crashing the diagnosis pipeline.
"""

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, PointIdsList, VectorParams, Distance
from sentence_transformers import SentenceTransformer
import os
import re
import uuid
import logging
import threading

logger = logging.getLogger("autofixops")

SUCCESSES_COLLECTION = "incident_resolutions"
FAILURES_COLLECTION = "incident_failures"
RUNBOOKS_COLLECTION = "operational_runbooks"
ALL_COLLECTIONS = (SUCCESSES_COLLECTION, FAILURES_COLLECTION, RUNBOOKS_COLLECTION)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

RUNBOOK_SOURCE_TYPE = "official_documentation"
RUNBOOK_SOURCE_STATUS = "curated"

# Fixed namespace so a runbook_id always maps to the same Qdrant point id,
# which makes re-seeding an overwrite instead of a duplicate.
_RUNBOOK_ID_NAMESPACE = uuid.UUID("5b2f6c1e-8a4d-4e5b-9c3a-7f1d2e6a9b40")

# Process-level singletons. Lazy-initialized on first use.
_client_singleton = None
_encoder_singleton = None
_init_lock = threading.Lock()


def describe_storage() -> str:
    """Human-readable description of where this process stores vectors."""
    if os.getenv("QDRANT_URL", "").strip():
        return f"server {os.getenv('QDRANT_URL').strip()}"
    if os.getenv("QDRANT_PATH", "").strip():
        return f"local path {os.path.abspath(os.getenv('QDRANT_PATH').strip())}"
    return "in-memory (not persisted)"


def is_persistent_storage() -> bool:
    return bool(os.getenv("QDRANT_URL", "").strip() or os.getenv("QDRANT_PATH", "").strip())


def _build_client() -> QdrantClient:
    url = os.getenv("QDRANT_URL", "").strip()
    if url:
        return QdrantClient(url=url, api_key=os.getenv("QDRANT_API_KEY") or None)
    path = os.getenv("QDRANT_PATH", "").strip()
    if path:
        return QdrantClient(path=path)
    # Default: in-memory store — avoids file-locking issues across
    # FastAPI + Celery worker processes on the same machine.
    return QdrantClient(location=":memory:")


def ensure_collections(client: QdrantClient):
    for collection in ALL_COLLECTIONS:
        if not client.collection_exists(collection_name=collection):
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )
            logger.info(f"[RAG] Initialized collection: {collection}")


def _get_client() -> QdrantClient:
    global _client_singleton
    if _client_singleton is None:
        with _init_lock:
            if _client_singleton is None:
                client = _build_client()
                ensure_collections(client)
                logger.info(f"[RAG] Qdrant storage: {describe_storage()}")
                _client_singleton = client
    return _client_singleton


def to_embedding_text(text: str) -> str:
    """Canonical text normalization applied to BOTH documents and queries.

    Lowercased, whitespace collapsed, and underscores split so identifiers
    like HIGH_CPU or TargetAppDown-style labels embed as words.
    """
    text = (text or "").replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def runbook_point_id(runbook_id: str) -> str:
    return str(uuid.uuid5(_RUNBOOK_ID_NAMESPACE, runbook_id))


def _get_encoder() -> SentenceTransformer:
    global _encoder_singleton
    if _encoder_singleton is None:
        with _init_lock:
            if _encoder_singleton is None:
                _encoder_singleton = SentenceTransformer(EMBEDDING_MODEL)
    return _encoder_singleton


class QdrantMemoryStore:
    """
    RAG memory:
      - incident_resolutions: what WORKED (verified fixes)
      - incident_failures:    what FAILED (with reason classification)
      - operational_runbooks: curated synthetic guidance (not incidents)
    """

    def __init__(self, storage_path: str = "./qdrant_data", client: QdrantClient = None):
        # storage_path kept for backward compatibility but unused —
        # storage is configured via QDRANT_URL / QDRANT_PATH (see module docstring).
        # `client` lets callers (tests) supply an isolated Qdrant client.
        try:
            if client is not None:
                ensure_collections(client)
                self.client = client
            else:
                self.client = _get_client()
            self.encoder = _get_encoder()
            self.available = True
        except Exception as e:
            logger.error(f"[RAG] Initialization failed, RAG disabled this run: {e}")
            self.client = None
            self.encoder = None
            self.available = False

    def store_resolution(self, summary_text: str, root_cause: str, successful_action: str):
        if not self.available:
            return
        try:
            vector = self._encode(summary_text)
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "summary": summary_text,
                    "root_cause_classification": root_cause,
                    "action_taken": successful_action,
                    "outcome": "SUCCESS",
                },
            )
            self.client.upsert(collection_name=SUCCESSES_COLLECTION, points=[point])
            logger.info("[RAG] Successful resolution stored.")
        except Exception as e:
            logger.error(f"[RAG] Failed to store resolution: {e}")

    def store_failure(
        self, summary_text: str, root_cause: str, failed_action: str, failure_reason: str
    ):
        if not self.available:
            return
        if failure_reason not in ("verification_failed",):
            logger.debug(
                f"[RAG] Skipping failure storage — '{failure_reason}' is not a verified execution failure."
            )
            return

        try:
            vector = self._encode(summary_text)
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "summary": summary_text,
                    "root_cause_classification": root_cause,
                    "action_taken": failed_action,
                    "failure_reason": failure_reason,
                    "outcome": "FAILED",
                },
            )
            self.client.upsert(collection_name=FAILURES_COLLECTION, points=[point])
            logger.info(f"[RAG] Failure pattern stored: {failure_reason}")
        except Exception as e:
            logger.error(f"[RAG] Failed to store failure: {e}")

    def _query(self, collection: str, query_text: str, top_k: int, score_threshold: float) -> list:
        """Common path for both retrieve_similar and retrieve_failures.
        Uses the modern qdrant-client query_points() API.
        """
        if not self.available:
            return []
        try:
            query_vector = self._encode(query_text)
            response = self.client.query_points(
                collection_name=collection,
                query=query_vector,
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
            )
            # query_points returns a QueryResponse with .points
            points = getattr(response, "points", None) or []
            return [p.payload for p in points if getattr(p, "payload", None)]
        except Exception as e:
            logger.warning(f"[RAG] Retrieval from '{collection}' failed (degrading gracefully): {e}")
            return []

    def retrieve_similar(self, query_text: str, top_k: int = 2) -> list:
        return self._query(SUCCESSES_COLLECTION, query_text, top_k, 0.8)

    def retrieve_failures(self, query_text: str, top_k: int = 2) -> list:
        return self._query(FAILURES_COLLECTION, query_text, top_k, 0.75)

    # ─── Curated runbooks (operational_runbooks collection only) ───

    def _encode(self, text: str) -> list:
        return self.encoder.encode(to_embedding_text(text)).tolist()

    def upsert_runbooks(self, runbooks: list) -> int:
        """Inserts or replaces curated runbooks. Idempotent: ids derive from runbook_id.

        Raises on failure — seeding is an explicit operator action, so errors
        should be loud rather than degraded.
        """
        if not self.available:
            raise RuntimeError("RAG store is unavailable; cannot seed runbooks.")

        points = []
        for runbook in runbooks:
            if runbook.get("source_type") != RUNBOOK_SOURCE_TYPE:
                raise ValueError(
                    f"Refusing to store '{runbook.get('runbook_id')}' as a runbook: "
                    f"source_type must be '{RUNBOOK_SOURCE_TYPE}'."
                )
            payload = {k: v for k, v in runbook.items() if k != "embedding_text"}
            points.append(PointStruct(
                id=runbook_point_id(runbook["runbook_id"]),
                vector=self._encode(runbook["embedding_text"]),
                payload=payload,
            ))

        self.client.upsert(collection_name=RUNBOOKS_COLLECTION, points=points)
        logger.info(f"[RAG] Upserted {len(points)} runbook(s) into '{RUNBOOKS_COLLECTION}'.")
        return len(points)

    def sync_runbooks(self, runbooks: list) -> tuple:
        """Makes operational_runbooks contain exactly `runbooks`. Returns (written, removed).

        Upserts the given documents, then deletes every other point in the
        collection (e.g. retired documents from an earlier knowledge base).
        Idempotent: a second run writes the same points and removes nothing.
        """
        written = self.upsert_runbooks(runbooks)
        keep = {runbook_point_id(r["runbook_id"]) for r in runbooks}
        stale, offset = [], None
        while True:
            points, offset = self.client.scroll(
                collection_name=RUNBOOKS_COLLECTION, limit=256, offset=offset,
                with_payload=False, with_vectors=False,
            )
            stale += [p.id for p in points if str(p.id) not in keep]
            if offset is None:
                break
        if stale:
            self.client.delete(collection_name=RUNBOOKS_COLLECTION, points_selector=PointIdsList(points=stale))
            logger.info(f"[RAG] Removed {len(stale)} retired document(s) from '{RUNBOOKS_COLLECTION}'.")
        return written, len(stale)

    def count(self, collection: str) -> int:
        if not self.available:
            return 0
        return self.client.count(collection_name=collection, exact=True).count

    def retrieve_runbooks(self, query_text: str, top_k: int = 3, min_score: float = None) -> list:
        """Returns [{"id", "score", "payload"}] best-first; [] on any failure."""
        if not self.available:
            return []
        try:
            response = self.client.query_points(
                collection_name=RUNBOOKS_COLLECTION,
                query=self._encode(query_text),
                limit=top_k,
                score_threshold=min_score,
                with_payload=True,
            )
            return [
                {"id": str(p.id), "score": float(p.score), "payload": p.payload}
                for p in (getattr(response, "points", None) or [])
            ]
        except Exception as e:
            logger.warning(f"[RAG] Runbook retrieval failed (degrading gracefully): {e}")
            return []
