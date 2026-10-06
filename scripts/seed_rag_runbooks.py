"""
Seed the RAG `operational_runbooks` collection with operational guidance curated
from official Kubernetes and Prometheus documentation (engine/runbooks.py).

Safe to re-run: each document has a deterministic id, so re-seeding replaces
the existing points instead of adding duplicates. Any other point in the
collection (for example the retired synthetic runbooks) is removed, so the
collection holds exactly the current knowledge base. Never touches the
incident_resolutions / incident_failures collections.

Requires persistent Qdrant storage (QDRANT_URL or QDRANT_PATH in .env or the
environment) — seeding an in-memory store would be lost when this script exits.
With QDRANT_PATH, stop the Celery worker first (one process at a time).

Usage:
    python scripts/seed_rag_runbooks.py
    python scripts/seed_rag_runbooks.py --query "the application keeps crashing"
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from engine.memory import (
    QdrantMemoryStore, RUNBOOKS_COLLECTION, describe_storage, is_persistent_storage,
)
from engine.runbooks import load_runbooks


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--query", help="After seeding, run a sample retrieval and print the results.")
    args = parser.parse_args()

    if not is_persistent_storage():
        print(
            "Refusing to seed: Qdrant is in-memory, so the documents would vanish when this "
            "script exits.\nSet QDRANT_PATH (e.g. ./qdrant_data) or QDRANT_URL "
            "(e.g. http://localhost:6333) and re-run."
        )
        sys.exit(1)

    print(f"Qdrant storage: {describe_storage()}")
    store = QdrantMemoryStore()
    if not store.available:
        print("RAG store failed to initialize (see log above).")
        sys.exit(1)

    documents = load_runbooks()
    written, removed = store.sync_runbooks(documents)
    print(f"Upserted {written} document(s), removed {removed} retired document(s); "
          f"'{RUNBOOKS_COLLECTION}' now holds {store.count(RUNBOOKS_COLLECTION)} point(s).")
    for d in documents:
        print(f"  - {d['runbook_id']}: {d['source_organization']} — {d['source_title']}")

    if args.query:
        print(f"\nTop matches for: {args.query!r}")
        for hit in store.retrieve_runbooks(args.query, top_k=3):
            p = hit["payload"]
            print(f"  {hit['score']:.3f}  {p['source_organization']} — {p['source_title']}  "
                  f"[{p['source_type']}/{p['source_status']}]  {p['source_url']}")

    store.client.close()


if __name__ == "__main__":
    main()
