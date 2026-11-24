#!/usr/bin/env bash
# Idempotent deploy, run ON the server from anywhere inside the repo checkout.
#   First run:   PUBLIC_BASE_URL=http://<elastic-ip> DASH_PASSWORD=... bash deploy/deploy.sh
#   Later runs:  bash deploy/deploy.sh          (CD calls this over AWS SSM)
# Secrets live only in deploy/.env.prod and deploy/secrets/ on the server (both git-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."
D=deploy
DC="docker compose -f $D/docker-compose.prod.yml --env-file $D/.env.prod"

if [ "${AUTOFIXOPS_PULL:-0}" = "1" ]; then
  git fetch --quiet origin main && git reset --hard "${AUTOFIXOPS_REF:-origin/main}"   # untracked deploy/.env.prod and secrets survive
fi

umask 077
mkdir -p $D/secrets
if [ ! -f $D/.env.prod ]; then
  : "${PUBLIC_BASE_URL:?set PUBLIC_BASE_URL on first run}"
  PGPW=$(openssl rand -hex 24)
  cat > $D/.env.prod <<ENV
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
POSTGRES_PASSWORD=${PGPW}
POSTGRES_URL=postgresql+psycopg2://autofixops:${PGPW}@postgres:5432/autofixops
MONGO_URL=
GITHUB_TOKEN=
GITHUB_REPO=
SHADOW_MODE=true
AI_INCIDENT_ANALYSIS=true
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_NUM_CTX=8192
OPENAI_API_KEY=
OPENAI_MODEL_NAME=gpt-4o-mini
RAG_RUNBOOK_MIN_SCORE=0.35
RAG_RUNBOOK_TOP_K=3
PIPELINE_STOP_AFTER_POLICY=false
PROMETHEUS_URL=http://prometheus:9090
REDIS_URL=redis://redis:6379/0
QDRANT_URL=http://qdrant:6333
QDRANT_PATH=
TARGET_MANIFEST_PATH=kubernetes_integration/target_app/deployment.yaml
ENV
fi
if [ -n "${DASH_PASSWORD:-}" ]; then
  printf 'admin:%s\n' "$(openssl passwd -apr1 "$DASH_PASSWORD")" > $D/secrets/htpasswd
fi
[ -s $D/secrets/htpasswd ] || { echo "missing $D/secrets/htpasswd (pass DASH_PASSWORD on first run)"; exit 1; }

$DC up -d --build --remove-orphans

echo "waiting for api health..."
for i in $(seq 1 60); do
  [ "$($DC ps --format '{{.Health}}' api)" = "healthy" ] && break
  sleep 5
done
[ "$($DC ps --format '{{.Health}}' api)" = "healthy" ] || { $DC logs --tail=80 api; echo "api not healthy"; exit 1; }

$DC exec -T api python scripts/migrate_schema.py
$DC exec -T ollama ollama pull "$(grep '^OLLAMA_MODEL=' $D/.env.prod | cut -d= -f2)"
$DC exec -T worker python scripts/seed_rag_runbooks.py || echo "WARN: runbook seeding failed (RAG will be empty)"
curl -fsS http://127.0.0.1/healthz
$DC ps
docker image prune -f >/dev/null
echo "deploy ok"
