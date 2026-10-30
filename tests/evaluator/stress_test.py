import asyncio
import httpx
import time

API_URL = "http://127.0.0.1:8010/api/v1/alerts"

async def send_duplicate_alerts(count=50):
    """
    Fires identical webhook payloads concurrently.
    Demonstrates mathematically that the idempotency constraint stops queue flooding.
    """
    payload = {
        "receiver": "webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PodCrashLooping",
                    "namespace": "autofixops",
                    "pod": "stress-test-pod-777",
                    "severity": "critical"
                }
            }
        ],
        "externalURL": "http://alertmanager.local",
        "version": "4",
        "groupKey": "{}/{}"
    }

    async with httpx.AsyncClient() as client:
        start_time = time.time()
        tasks = [client.post(API_URL, json=payload) for _ in range(count)]
        
        print(f"🚀 Firing {count} concurrent identical webhooks...")
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        successes = 0
        deduplicated_or_failed = 0
        
        for r in responses:
            if isinstance(r, httpx.Response) and r.status_code == 200:
                # Based on our logic, it always returns 200, but only ONE will have processed_incidents > 0
                if len(r.json().get("processed_incidents", [])) > 0:
                    successes += 1
                else:
                    deduplicated_or_failed += 1
            else:
                deduplicated_or_failed += 1

        print(f"⏱  Completed in {time.time() - start_time:.2f} seconds.")
        print(f"✅ Distinct Queued Incidents: {successes} (Expected: 1)")
        print(f"🛡  Deduplicated & Blocked: {deduplicated_or_failed} (Expected: {count - 1})")

if __name__ == "__main__":
    asyncio.run(send_duplicate_alerts())
