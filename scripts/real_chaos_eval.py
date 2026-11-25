"""
Real-chaos evaluation harness (no synthetic incidents).
=======================================================
Injects real faults into the real Kubernetes target through the public API, waits for the
incident that Prometheus -> Alertmanager -> AutoFixOps really creates, and records what the
pipeline did. The ground truth for every trial is the fault that was injected, so no author
ever labels a case by hand.

  AUTOFIXOPS_URL=http://<host> AUTOFIXOPS_USER=admin AUTOFIXOPS_PASSWORD=... \
  python scripts/real_chaos_eval.py --faults crash_loop memory_leak cpu_spike --repeats 3 \
         --out evaluation/real_chaos/run1.jsonl

Per trial it writes one JSON line: ground truth, timestamps (injection, incident created,
diagnosed), the incident row, its full context, and the remediation view. Between trials it
resets the fault and closes the incident so the next trial starts from a clean state.
Nothing here merges pull requests or edits the cluster directly; only /chaos/kubernetes,
/incidents/{id}/close and read-only GETs are used.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TRUTH = {"crash_loop": "CRASH_LOOP", "memory_leak": "MEMORY_LEAK", "cpu_spike": "CPU_SPIKE", "control": "NONE"}
SETTLED = {"DIAGNOSED", "ESCALATED", "FAILED", "RESOLVED", "POLICY_APPROVED", "PENDING_PR_MERGE", "VERIFIED"}
NOT_OPEN = {"RESOLVED", "FAILED", "CLOSED"}


class Api:
    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": self.auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except ValueError:
                return exc.code, None


def parse_utc(text):
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def target_ready(api):
    status, body = api.call("GET", "/api/v1/target/kubernetes")
    summary = ((body or {}).get("evidence") or {}).get("summary") or {}
    return status == 200 and summary.get("pods") and summary.get("ready_pods") == summary.get("pods")


def open_incidents(api):
    _, body = api.call("GET", "/api/v1/incidents?limit=200")
    return [i for i in (body or {}).get("incidents", []) if i["status"] not in NOT_OPEN]


def reset(api, settle_s):
    api.call("POST", "/api/v1/chaos/kubernetes", {"fault": "reset", "confirmation": "CONFIRM"})
    for i in open_incidents(api):
        api.call("POST", f"/api/v1/incidents/{i['id']}/close", {"reason": "real-chaos evaluation: trial finished"})
    deadline = time.time() + settle_s
    while time.time() < deadline:
        if target_ready(api):
            return True
        time.sleep(10)
    return False


def quiet(api, seconds):
    """Waits out the previous fault's tail (restart-count alerts clear minutes after the pod is healthy) and
    closes any incident it still raises, so the next trial's incident cannot be confused with the last one's."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        for i in open_incidents(api):
            api.call("POST", f"/api/v1/incidents/{i['id']}/close", {"reason": "real-chaos evaluation: previous trial's tail"})
        time.sleep(20)


def run_trial(api, fault, args):
    record = {"fault": fault, "ground_truth": TRUTH[fault], "started_utc": datetime.now(timezone.utc).isoformat()}
    if not reset(api, args.settle):
        record["error"] = "target did not become ready before the trial"
        return record
    quiet(api, args.quiet)                       # let Prometheus resolve the previous fault's alerts
    before = {i["id"] for i in open_incidents(api)}
    t_inject = datetime.now(timezone.utc)
    record["injected_utc"] = t_inject.isoformat()
    if fault != "control":
        status, body = api.call("POST", "/api/v1/chaos/kubernetes", {"fault": fault, "confirmation": "CONFIRM"})
        record["inject_response"] = {"status": status, "body": body}
        if status != 200:
            record["error"] = f"injection failed ({status})"
            return record
    incident = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(10)
        for i in open_incidents(api):
            if i["id"] not in before and parse_utc(i["created_at"]) >= t_inject - timedelta(seconds=10):
                incident = i
        if incident and incident["status"] in SETTLED:
            break
        if incident:   # keep the freshest row
            _, body = api.call("GET", "/api/v1/incidents?limit=200")
            incident = next((x for x in (body or {})["incidents"] if x["id"] == incident["id"]), incident)
            if incident["status"] in SETTLED:
                break
    if not incident:
        record["incident"] = None                # for a control trial this is the correct outcome
        return record
    iid = incident["id"]
    _, body = api.call("GET", "/api/v1/incidents?limit=200")
    record["incident"] = next((x for x in (body or {})["incidents"] if x["id"] == iid), incident)
    record["context"] = api.call("GET", f"/api/v1/incidents/{iid}/context")[1]
    record["remediation"] = api.call("GET", f"/api/v1/incidents/{iid}/remediation")[1]
    timeline = record["incident"].get("status_timeline") or []
    record["seconds_to_incident"] = round((parse_utc(record["incident"]["created_at"]) - t_inject).total_seconds(), 1)
    record["timeline"] = timeline
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--faults", nargs="+", default=["crash_loop", "memory_leak", "cpu_spike"], choices=sorted(TRUTH))
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=900, help="seconds to wait per trial for a settled incident")
    ap.add_argument("--settle", type=int, default=300, help="seconds to wait for a healthy target between trials")
    ap.add_argument("--quiet", type=int, default=45, help="seconds of calm before each injection")
    args = ap.parse_args()
    base, user, pw = os.environ.get("AUTOFIXOPS_URL"), os.environ.get("AUTOFIXOPS_USER"), os.environ.get("AUTOFIXOPS_PASSWORD")
    if not (base and user and pw):
        sys.exit("set AUTOFIXOPS_URL, AUTOFIXOPS_USER and AUTOFIXOPS_PASSWORD")
    api = Api(base, user, pw)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    order = [f for _ in range(args.repeats) for f in args.faults]    # interleave faults across repeats
    with open(args.out, "a", encoding="utf-8") as out:
        for n, fault in enumerate(order, 1):
            print(f"[{n}/{len(order)}] {fault}", flush=True)
            record = run_trial(api, fault, args)
            record["trial"] = n
            out.write(json.dumps(record) + "\n")
            out.flush()
            inc = record.get("incident") or {}
            print(f"    -> incident={inc.get('alert_name')} status={inc.get('status')} diagnosis={inc.get('diagnosis')} "
                  f"in {record.get('seconds_to_incident')}s {record.get('error', '')}", flush=True)
    reset(api, args.settle)


if __name__ == "__main__":
    main()
