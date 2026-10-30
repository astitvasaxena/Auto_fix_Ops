#!/bin/bash
# Trigger Memory Leak Chaos
# Requires the target app to be exposed locally
TARGET_URL=${1:-"http://localhost:8001"}

echo "🌊 Flooding memory at $TARGET_URL/leak ..."

# Send multiple requests to aggressively blow past the 100Mi boundary
for i in {1..12}; do
    echo "[req $i] Triggering chunk..."
    curl -s -X GET "$TARGET_URL/leak"
    sleep 0.5
done

echo "💀 Memory leak script finished. Check 'kubectl get pods -n autofixops -w' for OOMKilled status."
