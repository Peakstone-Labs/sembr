# Configuration

sembr uses `pydantic-settings` with a four-level priority chain (highest wins):

```
shell environment variables
    │
.env file
    │
sembr.yaml file (optional, in CWD)
    │
built-in defaults
```

There is **no `secrets_dir=`** support today — Docker secrets land via shell env vars on the container side, which is the same precedence level as plain env vars.

!!! warning
    Do not hardcode settings fields in `docker-compose.yml`'s `environment:` block. The block sits at the same precedence level as a host-side shell `export`, so pinning a field in compose silently masks any later `.env` change and breaks the runtime settings editor's apply-and-restart flow.

Per-intent and per-feed values (similarity threshold, scan interval, lookback, poll cadence, …) live on the `Intent` and `Feed` rows themselves and are managed via the REST API or the dashboard, not via environment variables. If a knob you want isn't here, look for it in the [api](modules/api.md) reference.

## Required

| Variable | Description |
|----------|-------------|
| `EMBEDDER_API_KEY` | SiliconFlow (or any OpenAI-compatible) API key for the `/v1/embeddings` endpoint. The container exits non-zero at startup if absent or blank. |

The same key is reused as `LLM_API_KEY` by default — SiliconFlow hosts both BGE-M3 and DeepSeek-V4-Flash, so one key is usually enough.

## Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant server URL. The bundled `docker-compose.yml` provisions this address |
| `SQLITE_PATH` | `/app/data/sembr.db` | SQLite database path inside the container. The host maps `./data/` here via the compose bind mount |
| `SEMBR_HOST_PORT` | `8000` | Host port exposed by Docker Compose. The in-container bind port is hardcoded to `8000` in the Dockerfile CMD; override the host side here |

## Embedder

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDER_BACKEND` | `siliconflow` | Embedding backend. Only `siliconflow` is shipped today |
| `EMBEDDER_API_BASE_URL` | `https://api.siliconflow.cn/v1` | OpenAI-compatible `/v1/embeddings` endpoint. Point at any provider that speaks the same protocol to swap |
| `EMBEDDER_MODEL` | `BAAI/bge-m3` | Model name passed to the endpoint |
| `EMBEDDER_TIMEOUT_SECONDS` | `30` | HTTP timeout for the startup probe and the httpx client default. Batch embed calls compute a dynamic timeout `max(30s, total_chars / 1500)`, so values below 30 do **not** tighten the batch path |

## LLM (summarizer)

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_API_BASE_URL` | `https://api.siliconflow.cn/v1` | OpenAI-compatible `/v1/chat/completions` endpoint |
| `LLM_API_KEY` | — | API key. Default-shares the SiliconFlow embedder key when left blank |
| `LLM_MODEL` | `deepseek-ai/DeepSeek-V4-Flash` | Model name passed to the chat completions endpoint |
| `LLM_TIMEOUT_SECONDS` | `60` | Per-request HTTP timeout |
| `LLM_MAX_PROMPT_CHARS` | `2_000_000` | Total prompt-side character budget (system + instruction + assembled articles). The pipeline reserves ~15 % for the response, then water-fills article bodies into the remainder — short articles stay whole, only the longest get truncated. Tune to your model's context window: `2_000_000` is generous for DeepSeek-V4-Flash (1 M-token ctx ≈ 2 M Chinese chars / 4 M English chars); drop to `~16_000` for an 8 K-token local model. Characters, not tokens — set conservatively for non-English content. Lower bound `2_000` |

Only the API-style backend (any `/v1/chat/completions` endpoint) ships today.

## Email notifier

Email is the only built-in notification channel today. Leave `SMTP_HOST` empty to disable email delivery; the rest of the app still runs.

| Variable | Default | Description |
|----------|---------|-------------|
| `SMTP_HOST` | `""` | SMTP server hostname (e.g. `smtp.gmail.com`, `smtp.sendgrid.net`). Empty disables email |
| `SMTP_PORT` | `587` | SMTP port. `587` for STARTTLS (default), `465` for `SMTP_SSL` |
| `SMTP_USERNAME` | `""` | SMTP login username. Empty skips `AUTH` |
| `SMTP_PASSWORD` | `""` | SMTP login password (`SecretStr`; never logged) |
| `SMTP_FROM` | `""` | `From:` address. Falls back to `SMTP_USERNAME` if empty |
| `SMTP_USE_STARTTLS` | `true` | Run `STARTTLS` after the plain SMTP connect |
| `SMTP_USE_SSL` | `false` | Use `SMTP_SSL` directly (port 465 style). When `true`, `SMTP_USE_STARTTLS` is ignored |

The per-intent timezone (`Intent.timezone`) is what the email template uses to render `published_at`; `DISPLAY_TIMEZONE` below is consulted by the dashboard, not by email rendering.

## Dashboard & logs

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_TOKEN` | `""` | Optional shared secret gating `/dashboard` and `/api/dashboard/*`. Empty disables auth — set this whenever the host is reachable beyond `localhost`, since feed URLs and dead-article error messages would otherwise be public |
| `DASHBOARD_POLL_INTERVAL_SECONDS` | `10` | Frontend snapshot polling cadence. Bounded `[2, 120]`. Surfaced via `/api/dashboard/config` to the bundled JS |
| `DASHBOARD_LOG_RETENTION_DAYS` | `7` | Maximum age of rows kept in `feed_fetch_log` and `embed_call_log`. Bounded `[1, 90]` |
| `DASHBOARD_LOG_MAX_PER_FEED` | `1000` | Per-feed FIFO cap on `feed_fetch_log` rows. Bounded `[10, 100000]` |
| `DASHBOARD_LOG_LEVEL` | `INFO` | Default level applied to all seven LogBus tags on startup. One of `DEBUG / INFO / WARNING / ERROR`. The dashboard's `PUT /api/dashboard/logs/level` can adjust each tag at runtime; runtime changes are process-memory only and reset on restart |
| `DASHBOARD_LOG_BUFFER_PER_TAG` | `1000` | Ring buffer capacity per log tag. Bounded `[100, 10000]`. Memory cost ≈ `7 × buffer × ~500 B`, so the max sits around 35 MB |

## Display

| Variable | Default | Description |
|----------|---------|-------------|
| `DISPLAY_TIMEZONE` | `Asia/Shanghai` | IANA timezone surfaced to the dashboard for timestamp rendering. **Not** consulted by the email notifier — that uses each intent's own `timezone` field |

## Prompts

| Variable | Default | Description |
|----------|---------|-------------|
| `PROMPTS_DIR` | `/app/prompts` | Root directory for prompt templates. Subdirectories: `system/` and `instruction/`. Template edits on the host take effect on the next tick — no restart needed. Override via `SEMBR_PROMPTS_DIR` |

## Lifespan / shutdown

| Variable | Default | Description |
|----------|---------|-------------|
| `LIFESPAN_SHUTDOWN_TIMEOUT` | `8.0` | Maximum seconds allowed for graceful lifespan shutdown before forcing exit. Set below docker-stop's SIGKILL deadline (default 10 s). Only applies to self-restart paths (e.g. settings save → SIGTERM); a normal `docker compose down` is not affected |

## Collector / RSSHub

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_HOSTS` | `rsshub:1200` | Comma-separated `host[:port]` entries that front many backends (the bundled RSSHub instance is the canonical example). For these hosts the per-host concurrency limiter additionally segments by the first URL path segment, so backends behind one proxy don't share a single semaphore |

## RSSHub passthrough variables

These environment variables are forwarded as-is to the bundled RSSHub container — they are read by RSSHub itself, not by sembr code. The settings editor accepts new keys that match `^[A-Z][A-Z0-9_]*$` and begin with one of the allowed prefixes (`TWITTER_`, `TELEGRAM_`, `GITHUB_`, `RSSHUB_`, `SOCIAL_`, `OPENAI_`).

| Variable | Used by | Notes |
|---|---|---|
| `TWITTER_COOKIE` | RSSHub Twitter routes | Full cookie value from a logged-in browser session — minimum `auth_token=...; ct0=...` |
| `TELEGRAM_TOKEN` | RSSHub Telegram routes | Bot token from BotFather, for public channel feeds |
| `TELEGRAM_SESSION` | RSSHub Telegram routes | User session string (Telethon / Pyrogram) for restricted channels |
| `GITHUB_ACCESS_TOKEN` | RSSHub GitHub routes | PAT — raises the API rate limit from 60 to 5000 req/h |
