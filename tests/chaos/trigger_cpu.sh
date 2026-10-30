#!/bin/bash
# Trigger CPU Spike Chaos
TARGET_URL=${1:-"http://localhost:8001"}

echo "🔥 Igniting CPU at $TARGET_URL/cpu ..."
echo "Note: This is an infinite loop inside the pod. It will hang curl."

curl -X GET "$TARGET_URL/cpu" --max-time 3

echo "✅ Hook invoked. The worker thread is now pinned."
echo "📉 Check 'kubectl get pods -n autofixops -w' to watch the health probe fail and trigger a restart."
