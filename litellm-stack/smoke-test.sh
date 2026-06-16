#!/usr/bin/env bash
# Quick smoke test against a running LiteLLM stack (port 4000).
# Requires LITELLM_MASTER_KEY in your environment (same value as litellm-stack/.env).
#
#   export LITELLM_MASTER_KEY=...
#   ./smoke-test.sh
set -euo pipefail
: "${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY first (matches litellm-stack/.env)}"

BASE="${LITELLM_BASE_URL:-http://localhost:4000}"

echo "--- liveness ---"
curl -s "$BASE/health/liveliness" && echo

echo "--- list agents via admin endpoint ---"
curl -s "$BASE/v1/agents" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python3 -m json.tool | head -50

echo "--- invoke developer agent (OpenAI-compatible route) ---"
curl -s "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
        "model": "a2a/developer",
        "messages": [{"role": "user", "content": "Design a JWT-based auth service for a multi-tenant FastAPI app. Keep the whole answer under 600 words."}]
      }' | python3 -m json.tool
