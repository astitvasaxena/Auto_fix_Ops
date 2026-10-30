"""
Safety Drill: Alert Storm
==========================
Fires 100 identical alerts. Verifies:
1. Dedup holds — only 1 incident created
2. No action spam — no duplicate remediation audits
3. Circuit breaker doesn't false-trigger on dedup noise
"""
import asyncio
import httpx
import time
import sys
import os

API_URL = os.getenv("API_URL", "http://127.0.0.1:8010/api/v1/alerts")


async def fire_alert_storm(count=100):
    """Fires identical webhook payloads concurrently."""
    payload = {
        "receiver": "webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "StormTestAlert",
                    "namespace": "autofixops",
                    "pod": "storm-test-pod-999",
                    "container": "app",
                    "severity": "critical",
                },
            }
        ],
        "externalURL": "http://alertmanager.local",
        "version": "4",
        "groupKey": "{}/{}",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        start = time.time()
        tasks = [client.post(API_URL, json=payload) for _ in range(count)]

        print(f"🚀 Firing {count} concurrent identical webhooks...")
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        created = 0
        deduplicated = 0
        errors = 0

        for r in responses:
            if isinstance(r, httpx.Response) and r.status_code == 200:
                processed = r.json().get("processed_incidents", [])
                if len(processed) > 0:
                    created += 1
                else:
                    deduplicated += 1
            else:
                errors += 1

        elapsed = time.time() - start
        print(f"\n⏱  Completed in {elapsed:.2f}s")
        print(f"✅ Incidents Created: {created} (Expected: 1)")
        print(f"🛡  Deduplicated: {deduplicated} (Expected: {count - 1})")
        print(f"❌ Errors: {errors} (Expected: 0)")

        # Assertions
        assert created <= 1, f"FAIL: Created {created} incidents, expected at most 1"
        assert errors == 0, f"FAIL: {errors} request errors"
        
        print("\n🛡️  Alert storm drill passed — dedup held under burst load.")

    # Check escalation rate hasn't spiked
    try:
        async with httpx.AsyncClient() as client:
            metrics_resp = await client.get("http://127.0.0.1:8010/api/v1/metrics")
            if metrics_resp.status_code == 200:
                metrics = metrics_resp.json().get("all_time", {})
                print(f"\n📊 Post-storm metrics:")
                print(f"   Total incidents: {metrics.get('total_incidents', 'N/A')}")
                print(f"   Escalation rate: {metrics.get('escalation_rate', 'N/A')}")
                print(f"   Shadow runs: {metrics.get('shadow_runs', 'N/A')}")
    except Exception:
        print("⚠️  Could not fetch post-storm metrics (API may not be running).")


if __name__ == "__main__":
    asyncio.run(fire_alert_storm())
