# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - TBD

Initial public release of **sembr** — semantic news monitoring via reverse RAG.
You write a natural-language intent once, sembr scans incoming articles, scores
them against the intent vector, and pushes matched-story digests to your inbox.

### Added

- **Reverse-RAG matcher** — per-intent cron scan (`scan_once` via Qdrant
  `query_points`) plus event-mode in-process scoring for sub-minute latency.
  Two-layer dedup: MD5 fingerprint on ingest, per-intent `match_seen` to
  prevent re-firing the same article across overlapping scan windows.
- **RSS collector** — `feedparser`-based source adapter, 23 pre-loaded feeds,
  per-feed APScheduler jobs with host-rate-limit guards. `BaseSource` ABC for
  future source types.
- **Embeddings** — BGE-M3 (1024-dim, 8192 ctx) via SiliconFlow's
  OpenAI-compatible `/v1/embeddings` API, batch-32 async client, startup probe
  surfaced through `/health`. Collections versioned as
  `news_{model}_{version}` behind the `news_current` / `intents_current`
  aliases for hot-swap embedding upgrades.
- **Qdrant vector store** — dual-collection design (intents + news), scalar
  int8 quantization with `always_ram=True` keeping ANN search in memory while
  raw vectors stay on disk. `qdrant/qdrant:v1.17.1` server, async client.
- **LLM summaries** — `APIBackend` against any OpenAI-compatible
  `/v1/chat/completions` endpoint (default DeepSeek-V4-Flash on SiliconFlow);
  Jinja2 prompts with strict-placeholder render and water-fill body packing
  against `BaseLLMBackend.max_prompt_chars`.
- **Email digest channel** — SMTP multipart/related with inline assets,
  per-intent recipient list. `BaseChannel` marker ABC ready for Telegram /
  Discord / Slack post-1.0.
- **Intent CRUD** — REST API for intents (cron and event modes), per-intent
  similarity threshold (default 0.75, range 0.60–0.95), feed-whitelist filter,
  channel discriminated union. PUT clears `match_seen` rows on text change.
- **Dashboard** — FastAPI + server-rendered HTML + SSE live tile updates;
  Articles / Intents / Logs / Settings tabs. In-app `.env` editor with
  restart-on-save through a `RestartController` driving
  `docker compose up -d --force-recreate --no-deps rsshub` over the mounted
  docker socket.
- **Runtime settings editor** — pydantic-settings five-level resolution
  (shell env > `.env` > `sembr.yaml` > defaults), Settings tab writes back to
  `.env` with dry-run validation before persist.
- **`/health` endpoint** — auth-free liveness probe reporting per-component
  state (`embedder`, `qdrant`, `db`, `scheduler`).
- **Public-deployment guide** — `docs/deployment/public.md` with reverse-proxy
  samples (Caddy / nginx / Cloudflare Tunnel), `ufw` and SSH hardening
  steps, and a `nmap`-based verification checklist for users self-hosting on
  cloud VMs.
- **Logbus** — in-process log ring buffer with SSE fan-out powering the
  dashboard Logs tab without writing to disk.

### Security

- **Dashboard token authentication** — `DashboardTokenMiddleware` gates
  `/api/*` behind `X-Dashboard-Token`. `/health` is intentionally auth-free.
- **Empty-token startup warning** — sembr logs an `ERROR` if
  `DASHBOARD_TOKEN` is empty; this is OK for local-only development but unsafe
  for any host reachable beyond localhost.
- **Documented docker-socket-mount risk** — the in-app RSSHub restart feature
  relies on a mounted docker socket; README and `docs/deployment/public.md`
  describe the single-tenant trade-off and how to disable the mount if the
  feature isn't needed.
- **Private Vulnerability Reporting** enabled on the repo; see
  [`SECURITY.md`](SECURITY.md).

### Tech stack

- Python 3.12, FastAPI 0.115.14, Pydantic v2, APScheduler 3.11.2,
  Qdrant 1.17.1, aiosqlite 0.20, httpx 0.27, feedparser 6.
- SQLite WAL mode with `synchronous=NORMAL`, `cache_size=-64000`,
  `busy_timeout=5000`; bind-mounted to host `./data/`.
- Apache-2.0 license; SPDX header required on every `.py` (CI strict gate).

[Unreleased]: https://github.com/Peakstone-Labs/sembr/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Peakstone-Labs/sembr/releases/tag/v1.0.0
