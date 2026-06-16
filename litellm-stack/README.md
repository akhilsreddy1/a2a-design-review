# LiteLLM stack

The central hub for the multi-agent system. LiteLLM plays three roles at once:

- **A2A agent registry** — agents register their Agent Cards here on startup.
- **A2A gateway** — peer consultations route through `/v1/a2a/{peer}/message/send`.
- **LLM proxy** — every agent's Claude calls go through `/v1/chat/completions`.

It runs as its **own Docker Compose stack** (LiteLLM + Postgres) on port `4000`,
deliberately separate from the agents so it stays up while you rebuild agent
containers.

## Start it

```bash
cd litellm-stack
cp .env.example .env          # then edit .env with your real keys
docker compose up -d
```

Set two values in `.env` (it is gitignored — never commit real keys):

| Variable | What it is |
|---|---|
| `LITELLM_MASTER_KEY` | The key clients authenticate with. Generate one with `openssl rand -hex 24`. **Must match `litellm_api_key`** in the agents' `.env` at the repo root. |
| `ANTHROPIC_API_KEY` | Your Anthropic key — used by the proxy to call Claude. |

## Verify

```bash
# liveness
curl -s http://localhost:4000/health/liveliness

# list registered agents (after the agents have started)
curl -s http://localhost:4000/v1/agents \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python3 -m json.tool
```

A quick end-to-end check is in [`smoke-test.sh`](smoke-test.sh):

```bash
export LITELLM_MASTER_KEY=...   # same value as in .env
./smoke-test.sh
```

## Then start the agents

With the hub running, start the agents + bridge from the repo root (see the main
[`README.md`](../README.md)). They register with this hub on startup and reach it
at `http://host.docker.internal:4000`. Make sure `litellm_api_key` in the repo-root
`.env` equals `LITELLM_MASTER_KEY` here.

## Notes

- **`config.yaml`** declares the Claude models and reads all secrets via
  `os.environ/...` — never hard-code keys there.
- **`STORE_MODEL_IN_DB=True`** lets you add models through the LiteLLM admin UI.
- The Postgres password (`dbpassword9090`) is LiteLLM's documented example default
  and is only used inside the compose network. Change `POSTGRES_PASSWORD` and the
  matching `DATABASE_URL` for anything beyond local development.
