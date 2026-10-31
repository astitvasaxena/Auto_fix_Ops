"""
RAG Drill: Official-Documentation Knowledge Base
=================================================
Verifies the curated knowledge base derived from official Kubernetes and
Prometheus documentation: schema and source metadata, idempotent seeding that
retires the old synthetic runbooks, and retrieval with metadata + scores.

Offline only: uses the locally cached all-MiniLM-L6-v2 model (HF_HUB_OFFLINE)
and isolated in-memory / temporary Qdrant stores. No Docker, Postgres, Redis,
OpenAI, GitHub or network access (source URLs are checked, never fetched).
"""
import os
import sys
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from engine.memory import (
    QdrantMemoryStore, EMBEDDING_DIM, FAILURES_COLLECTION, RUNBOOKS_COLLECTION,
    SUCCESSES_COLLECTION, ensure_collections, runbook_point_id, to_embedding_text,
)
from engine.runbooks import DISCLAIMER, OFFICIAL_SOURCES, load_runbooks

EXPECTED_DOCUMENTS = 6
OFFICIAL_URLS = {url for _, _, url in OFFICIAL_SOURCES}
OFFICIAL_HOSTS = {"kubernetes.io", "prometheus.io"}
SECTIONS = ("symptom", "operational_guidance", "evidence_to_collect", "possible_interpretations",
            "diagnostic_steps", "remediation_considerations", "safety_limitations")
# The five retired synthetic runbooks, with the ids the previous seed script used.
LEGACY_SYNTHETIC_IDS = ("runbook-target-app-down", "runbook-crash-loop", "runbook-high-cpu",
                        "runbook-high-memory", "runbook-deployment-failure")


def _fresh_store():
    """A store on its own in-memory client, isolated from the process singleton."""
    store = QdrantMemoryStore(client=QdrantClient(location=":memory:"))
    assert store.available, "RAG store failed to initialize"
    return store


def _seeded_store():
    store = _fresh_store()
    store.sync_runbooks(load_runbooks())
    return store


def _add_legacy_synthetic_points(client):
    """Writes points shaped like the retired synthetic runbooks (as the old seed did)."""
    ensure_collections(client)
    client.upsert(RUNBOOKS_COLLECTION, points=[
        PointStruct(id=runbook_point_id(rid), vector=[0.01] * EMBEDDING_DIM,
                    payload={"runbook_id": rid, "source_type": "synthetic_runbook",
                             "source_status": "curated", "verified": False})
        for rid in LEGACY_SYNTHETIC_IDS
    ])


def _all_payloads(client):
    points, _ = client.scroll(RUNBOOKS_COLLECTION, limit=100, with_payload=True)
    return [p.payload for p in points]


def _top(store, query, k=3):
    return store.retrieve_runbooks(query, top_k=k)


# ─── Knowledge base content ───

def test_expected_number_of_documents():
    docs = load_runbooks()
    assert len(docs) == EXPECTED_DOCUMENTS
    assert len({d["runbook_id"] for d in docs}) == EXPECTED_DOCUMENTS
    assert {d["source_organization"] for d in docs} == {"Kubernetes", "Prometheus"}
    print(f"✅ PASS: {EXPECTED_DOCUMENTS} official-source documents with unique ids")


def test_source_labels():
    for d in load_runbooks():
        assert d["source_type"] == "official_documentation", d["runbook_id"]
        assert d["source_status"] == "curated"
        assert d["verified"] is True
        assert d["guidance_type"] == "operational_guidance"
        assert d["disclaimer"] == DISCLAIMER
        assert "not a historical incident" in d["disclaimer"]
        assert "not an automatic remediation rule" in d["disclaimer"]
    print("✅ PASS: All documents are official_documentation / curated / verified=true")


def test_official_source_urls():
    for d in load_runbooks():
        urls = [d["source_url"]] + [s["url"] for s in d["additional_sources"]]
        for url in urls:
            parsed = urlparse(url)
            assert parsed.scheme == "https", url
            assert parsed.hostname in OFFICIAL_HOSTS, url
            assert url in OFFICIAL_URLS, f"{url} is not one of the approved official sources"
        assert d["source_title"] and d["source_url"] in d["content"]
        org = urlparse(d["source_url"]).hostname.split(".")[0]
        assert d["source_organization"].lower() == org, (d["source_organization"], d["source_url"])
    print("✅ PASS: Every source URL is an approved https kubernetes.io / prometheus.io page")


def test_document_structure():
    for d in load_runbooks():
        for section in SECTIONS:
            assert d["sections"][section], f"{d['runbook_id']} has empty section {section}"
        assert d["topic"] and d["incident_types"] and d["incident_type"] == d["incident_types"][0]
        content = d["content"]
        assert DISCLAIMER in content
        for heading in ("Symptom", "Evidence to collect", "Possible interpretations",
                        "Recommended diagnostic steps", "Remediation considerations", "Safety limitations"):
            assert f"## {heading}" in content, (d["runbook_id"], heading)
    print("✅ PASS: Each document follows symptom → evidence → interpretation → steps → remediation → safety")


def test_no_automatic_fix_rules():
    """Guidance must not read as 'symptom → automatically do X'."""
    for d in load_runbooks():
        text = d["content"].lower()
        assert "automatically" not in text.replace("not an automatic remediation rule", ""), d["runbook_id"]
    memory = next(d for d in load_runbooks() if d["runbook_id"] == "k8s-oomkilled-memory-pressure")
    assert any("does NOT prove" in i for i in memory["sections"]["possible_interpretations"])
    print("✅ PASS: No automatic-fix rules; memory guidance states usage alone does not prove OOM/leak")


def test_no_synthetic_documents_in_knowledge_base():
    assert not [d for d in load_runbooks() if d["source_type"] == "synthetic_runbook"]
    print("✅ PASS: No synthetic_runbook documents in the active knowledge base")


# ─── Embeddings and seeding ───

def test_embeddings_generated():
    store = _fresh_store()
    vector = store._encode(load_runbooks()[0]["embedding_text"])
    assert len(vector) == EMBEDDING_DIM and any(v != 0 for v in vector)
    assert to_embedding_text("  HIGH_CPU\n Alert ") == "high cpu alert"
    print(f"✅ PASS: Embeddings generated ({EMBEDDING_DIM} dims) with shared text normalization")


def test_seeding_twice_has_no_duplicates():
    store = _fresh_store()
    assert store.sync_runbooks(load_runbooks()) == (EXPECTED_DOCUMENTS, 0)
    assert store.sync_runbooks(load_runbooks()) == (EXPECTED_DOCUMENTS, 0)
    assert store.count(RUNBOOKS_COLLECTION) == EXPECTED_DOCUMENTS
    print(f"✅ PASS: Seeding twice leaves exactly {EXPECTED_DOCUMENTS} points (nothing duplicated)")


def test_sync_replaces_legacy_synthetic_runbooks():
    client = QdrantClient(location=":memory:")
    _add_legacy_synthetic_points(client)
    store = QdrantMemoryStore(client=client)
    assert store.count(RUNBOOKS_COLLECTION) == 5
    written, removed = store.sync_runbooks(load_runbooks())
    assert (written, removed) == (EXPECTED_DOCUMENTS, 5)
    payloads = _all_payloads(client)
    assert len(payloads) == EXPECTED_DOCUMENTS
    assert {p["source_type"] for p in payloads} == {"official_documentation"}
    print("✅ PASS: Sync removes the 5 legacy synthetic runbooks and keeps only official documents")


def test_incident_collections_untouched():
    store = _seeded_store()
    assert store.count(SUCCESSES_COLLECTION) == 0
    assert store.count(FAILURES_COLLECTION) == 0
    print("✅ PASS: incident_resolutions / incident_failures stay empty after seeding")


def test_non_official_document_rejected():
    store = _fresh_store()
    for bad_type in ("synthetic_runbook", "historical_incident"):
        try:
            store.upsert_runbooks([dict(load_runbooks()[0], source_type=bad_type)])
            assert False, f"accepted source_type={bad_type}"
        except ValueError as e:
            assert "official_documentation" in str(e)
    assert store.count(RUNBOOKS_COLLECTION) == 0
    print("✅ PASS: Documents without source_type=official_documentation are refused")


# ─── Retrieval ───

def test_crash_query_retrieves_crash_guidance():
    hits = _top(_seeded_store(), "target application is crashing repeatedly")
    assert hits[0]["payload"]["runbook_id"] == "k8s-repeated-container-failure", hits
    assert hits[0]["payload"]["source_url"].startswith("https://kubernetes.io/docs/tasks/debug/")
    print(f"✅ PASS: Crash query → {hits[0]['payload']['source_title']} ({hits[0]['score']:.3f})")


def test_memory_query_retrieves_memory_guidance():
    hits = _top(_seeded_store(), "memory usage is high and container restarted")
    assert hits[0]["payload"]["runbook_id"] == "k8s-oomkilled-memory-pressure", hits
    assert "assign-memory-resource" in hits[0]["payload"]["source_url"]
    print(f"✅ PASS: Memory query → {hits[0]['payload']['source_title']} ({hits[0]['score']:.3f})")


def test_cpu_query_retrieves_cpu_guidance():
    hits = _top(_seeded_store(), "CPU usage is high")
    assert hits[0]["payload"]["runbook_id"] == "k8s-cpu-limits-throttling", hits
    assert "manage-resources-containers" in hits[0]["payload"]["source_url"]
    print(f"✅ PASS: CPU query → {hits[0]['payload']['source_title']} ({hits[0]['score']:.3f})")


def test_target_down_query_retrieves_availability_guidance():
    hits = _top(_seeded_store(), "target is down")
    assert hits[0]["payload"]["runbook_id"] == "prometheus-target-availability", hits
    assert hits[0]["payload"]["source_organization"] == "Prometheus"
    print(f"✅ PASS: Target-down query → {hits[0]['payload']['source_title']} ({hits[0]['score']:.3f})")


def test_evidence_queries_retrieve_cpu_and_memory_guidance():
    # A "target down" alert's own text outweighs the cause; the evidence-only query (production
    # top_k 3 and min_score 0.35) must still reach the guidance the Stage 2 proposer requires.
    from engine.ai_diagnostics import build_evidence_query, retrieve_runbook_evidence

    store = _seeded_store()
    starved = build_evidence_query({"telemetry": {"summary": {"cpu_peak": 0.49}}, "kubernetes": {"summary": {
        "ready_pods": 0, "readiness_failures": 6, "restart_count": 0, "live_cpu_limit": "500m"}}})
    leak = build_evidence_query({"telemetry": {"memory_growth_mib_per_min": 30.0}, "kubernetes": {"summary": {}}})
    for query, expected in ((starved, "k8s-cpu-limits-throttling"), (leak, "k8s-oomkilled-memory-pressure")):
        hits, status = retrieve_runbook_evidence(store, query, 3, 0.35)
        assert status == "ok" and hits[0]["runbook_id"] == expected, (query, hits)
    print("✅ PASS: evidence queries retrieve the CPU-throttling and memory guidance first")


def test_results_carry_source_metadata_and_scores():
    hits = _top(_seeded_store(), "the app is down and health checks fail", k=EXPECTED_DOCUMENTS)
    assert len(hits) == EXPECTED_DOCUMENTS
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    for h in hits:
        assert isinstance(h["score"], float) and -1.0 <= h["score"] <= 1.0
        p = h["payload"]
        for field in ("source_organization", "source_title", "source_url", "topic", "title",
                      "incident_types", "content", "disclaimer"):
            assert p.get(field), (p.get("runbook_id"), field)
        assert p["source_type"] == "official_documentation" and p["verified"] is True
        assert "embedding_text" not in p
    store = _seeded_store()
    assert store.retrieve_runbooks("target is down", top_k=5, min_score=0.99) == []
    print("✅ PASS: Results include id, float score (best-first) and full source metadata")


def test_seed_script_replaces_legacy_and_is_idempotent():
    """Runs the real seed script twice against a temporary on-disk store holding legacy points."""
    tmp = tempfile.mkdtemp(prefix="autofixops_qdrant_")
    try:
        legacy = QdrantClient(path=tmp)
        _add_legacy_synthetic_points(legacy)
        legacy.close()

        env = dict(os.environ, QDRANT_PATH=tmp, QDRANT_URL="", PYTHONIOENCODING="utf-8")
        script = os.path.join(REPO_ROOT, "scripts", "seed_rag_runbooks.py")
        expected_removed = (5, 0)
        for removed in expected_removed:
            run = subprocess.run([sys.executable, script], env=env, cwd=REPO_ROOT,
                                 capture_output=True, text=True, timeout=300)
            assert run.returncode == 0, run.stdout + run.stderr
            assert f"removed {removed} retired document(s)" in run.stdout, run.stdout
            assert f"now holds {EXPECTED_DOCUMENTS} point(s)" in run.stdout, run.stdout

        client = QdrantClient(path=tmp)
        try:
            payloads = _all_payloads(client)
            assert len(payloads) == EXPECTED_DOCUMENTS
            assert {p["source_type"] for p in payloads} == {"official_documentation"}
            assert client.count(SUCCESSES_COLLECTION, exact=True).count == 0
        finally:
            client.close()

        refused = subprocess.run([sys.executable, script], cwd=REPO_ROOT, timeout=300,
                                 env=dict(env, QDRANT_PATH=""), capture_output=True, text=True)
        assert refused.returncode == 1 and "Refusing to seed" in refused.stdout
        print("✅ PASS: Seed script replaces legacy runbooks, stays at 6 on re-run, refuses in-memory storage")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("RAG DRILL: Official-Documentation Knowledge Base")
    print("=" * 60 + "\n")
    test_expected_number_of_documents()
    test_source_labels()
    test_official_source_urls()
    test_document_structure()
    test_no_automatic_fix_rules()
    test_no_synthetic_documents_in_knowledge_base()
    test_embeddings_generated()
    test_seeding_twice_has_no_duplicates()
    test_sync_replaces_legacy_synthetic_runbooks()
    test_incident_collections_untouched()
    test_non_official_document_rejected()
    test_crash_query_retrieves_crash_guidance()
    test_memory_query_retrieves_memory_guidance()
    test_cpu_query_retrieves_cpu_guidance()
    test_target_down_query_retrieves_availability_guidance()
    test_results_carry_source_metadata_and_scores()
    test_seed_script_replaces_legacy_and_is_idempotent()
    print("\n🎯 ALL CHECKS PASSED\n")
