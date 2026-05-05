# Configuration

sembr uses `pydantic-settings` with a 5-level priority chain (highest wins):

```
runtime API overrides
    │
Docker secrets
    │
environment variables
    │
.env file
    │
sembr.yaml file
    │
built-in defaults
```

!!! warning
    Do not hardcode settings fields in `docker-compose.yml`'s `environment:` block — it bypasses the priority chain and breaks runtime API overrides.

## Required

| Variable | Description |
|----------|-------------|
| `EMBEDDER_API_KEY` | SiliconFlow (or OpenAI-compatible) API key. Container exits at startup if absent. |

## Embedder

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDER_API_BASE_URL` | `https://api.siliconflow.cn/v1` | OpenAI-compatible `/v1/embeddings` endpoint |
| `EMBEDDER_MODEL` | `BAAI/bge-m3` | Embedding model name |
| `EMBEDDER_BATCH_SIZE` | `32` | Articles per embedding request |

## Collector

| Variable | Default | Description |
|----------|---------|-------------|
| `COLLECTOR_POLL_INTERVAL_MINUTES` | `30` | Default RSS poll interval |
| `COLLECTOR_LOOKBACK_HOURS` | `24` | How far back to fetch on first run |

## Matcher

| Variable | Default | Description |
|----------|---------|-------------|
| `MATCHER_INTERVAL_MINUTES` | `5` | How often the matcher job runs |
| `MATCHER_DEFAULT_THRESHOLD` | `0.75` | Default similarity threshold (0.20–0.95) |
| `MATCHER_LOOKBACK_MINUTES` | `60` | Only match articles ingested within this window |

## Notifier

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `DISCORD_WEBHOOK_URL` | — | Discord incoming webhook URL |
| `SLACK_WEBHOOK_URL` | — | Slack incoming webhook URL |

## Dashboard

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_TOKEN` | — | Auth token for `/dashboard` and `/api/dashboard/*`. Empty = no auth (LAN-only safe). |

## Infrastructure

| Variable | Default | Description |
|----------|---------|-------------|
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant server URL |
| `DATABASE_PATH` | `/app/data/sembr.db` | SQLite database file path |
| `API_PORT` | `8000` | In-container bind port (do not change) |
| `SEMBR_HOST_PORT` | `8000` | Host-side exposed port |

## LLM (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND` | `api` | `api` or `local` |
| `LLM_API_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI-compatible chat completions endpoint |
| `LLM_API_KEY` | — | API key for the LLM backend |
| `LLM_MODEL` | `deepseek-chat` | Model name |
