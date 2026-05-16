# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-05-16

Initial public release of **sembr** — a self-hosted intent radar built on
Reverse RAG. Write a natural-language intent once; sembr continuously scans
RSS / NewsAPI / Twitter, vector-matches new articles to the intent, and pushes
LLM-analyzed digests to your inbox on whatever schedule you set.

### Added

- **Reverse-RAG matcher** — per-intent cron scan (`scan_once` via Qdrant
  `query_points`) plus event-mode in-process scoring for sub-minute latency.
  Two-layer dedup: MD5 fingerprint on ingest, per-intent `match_seen` to
  prevent re-firing the same article across overlapping scan windows.
- **RSS collector** — `feedparser`-based source adapter with **22 curated
  RSS feeds** pre-loaded (Guardian, SCMP, NPR, Washington Post, Bloomberg
  Markets, 华尔街见闻, 第一财经, 36氪, 虎嗅, 财联社电报, 澎湃, 国家统计局,
  Nature ×3, HelloGitHub, etc. — mixed EN / CN). Per-feed APScheduler jobs
  with host-rate-limit guards. `BaseSource` ABC for future source types.
- **NewsAPI.ai aggregator collector** — async collector covering **30
  pre-configured English aggregator sources** (Reuters, BBC, NYT, WSJ, FT,
  Economist, Atlantic, NPR, TechCrunch, Wired, Ars Technica, Vox, …). A
  single master tick polls all NewsAPI feeds with one shared token;
  category whitelist + server-side dedup disabled so client-side MD5
  fingerprinting handles cross-source duplicates. Free signup token covers
  ~30 days at the default 30-min cadence.
- **Twitter via RSSHub** — bundled `diygod/rsshub` sidecar exposes Twitter
  user timelines and keyword searches as RSS routes. One Twitter feed
  pre-loaded (Elon Musk); extend by setting `TWITTER_AUTH_TOKEN`. RSSHub
  also serves most pre-loaded Chinese sources (财联社, 华尔街见闻, 36氪,
  Nature, etc.) so JS-rendered origins work out of the box. **53 total
  pre-loaded sources across the three source types.**
- **Embeddings** — BGE-M3 (1024-dim, 8192 ctx) via SiliconFlow's
  OpenAI-compatible `/v1/embeddings` API, batch-32 async client, startup
  probe surfaced through `/health`. Collections versioned as
  `news_{model}_{version}` behind the `news_current` / `intents_current`
  aliases for hot-swap embedding upgrades. Free at any volume on
  SiliconFlow's BGE-M3 tier.
- **Qdrant vector store** — dual-collection design (intents + news), scalar
  int8 quantization with `always_ram=True` keeping ANN search in memory
  while raw vectors stay on disk. `qdrant/qdrant:v1.17.1` server, async
  client.
- **LLM digest generation** — `APIBackend` against any OpenAI-compatible
  `/v1/chat/completions` endpoint (default DeepSeek-V4-Flash on SiliconFlow,
  1 M-token context window). Jinja2 prompts with strict-placeholder render
  and water-fill body packing against `BaseLLMBackend.max_prompt_chars` —
  one digest can ingest a hundred long-form articles for well under a
  cent at default pricing.
- **Custom prompt templates** — system + instruction templates with
  strict-placeholder validation, dashboard CRUD (create / duplicate / edit
  / rename / delete), per-intent template selection, and reference-counting
  to prevent orphaned references. Edits take effect on the next scheduler
  tick — no restart needed.
- **Email digest channel** — SMTP multipart/related with inline assets,
  per-intent recipient list, matcher-score badges per article, rendered in
  each intent's own timezone. `BaseChannel` marker ABC ready for Telegram /
  Discord / Slack post-1.0.
- **Intent CRUD** — REST API for intents (cron and event modes), per-intent
  similarity threshold (default 0.75, range 0.60–0.95), feed-whitelist
  filter, channel discriminated union, per-intent system + instruction
  template selection. PUT clears `match_seen` rows on text change.
- **External-fire API for agents** — `POST /api/external/intents/{id}/fire`
  is a synchronous endpoint that returns matched articles + LLM summary as
  JSON in one round-trip. Per-call overrides for `lookback_seconds`,
  `threshold`, and `feed_ids`; no notification side-effects and no
  `match_seen` writes; rate-limit 1/intent/60s; error strings scrubbed
  before egress (no paths / URLs / tracebacks leak). Lets external agent
  stacks (Hermes, OpenClaw, LangGraph, custom orchestrators) treat sembr
  as a tool node.
- **Dashboard** — FastAPI + server-rendered HTML + SSE live tile updates;
  Feeds / Intents / Templates / Articles / Logs / Settings tabs. Per-feed
  fetch outcomes (with 24-hour sparkline), embedder latency, per-container
  CPU / memory / uptime, Qdrant article browser with date / source / title
  filters, live log SSE, and a one-click api + rsshub restart trigger
  gated by `DASHBOARD_TOKEN`.
- **Runtime settings editor** — pydantic-settings four-level resolution
  (shell env > `.env` > `sembr.yaml` > defaults). Settings tab writes back
  to `.env` with dry-run validation, then drives
  `docker compose up -d --force-recreate --no-deps` through a
  `RestartController` so configuration changes apply without a manual
  restart.
- **`/health` endpoint** — auth-free liveness probe reporting per-component
  state (`embedder`, `qdrant`, `db`, `scheduler`).
- **Agent-driven install + skill kit (`agent/`)** — repo-level `agent/`
  directory grouping the artifacts an AI coding agent (Claude Code,
  Cursor, Cline, Aider, Continue, Roo) needs to deploy sembr and then
  drive it end-to-end:
  - `agent/INSTALL.md` — six-phase install protocol: hardware self-
    check, dependency install with consent, clone + parallel image
    pulls while the user fetches API keys, `.env` configuration,
    bring-up and `/health` polling, optional first intent. Includes a
    Phase 4 access-mode question (localhost / LAN / public + agent-
    yes-no), troubleshooting matrix, and agent guardrails.
  - `agent/sembr/` — [Agent Skills](https://agentskills.io) bundle
    (`SKILL.md` + `references/{endpoints,schemas,recipes,errors}.md`)
    teaching agents to drive a running sembr instance: auth
    (`X-Dashboard-Token`), endpoint surface, `IntentCreate` /
    `FeedCreate` schemas, curl + Python `httpx` recipes including the
    sync `POST /api/external/intents/{id}/fire` flow (no notifier
    side-effect, returns matches + LLM summary). Copy the folder into
    `~/.claude/skills/sembr/` for auto-loading, or hand the agent the
    `SKILL.md` directly.
  - `agent/PUBLIC_INSTALL.md` — agent-driven public-exposure
    sub-flow, invoked as a branch from `INSTALL.md` Phase 4 option C
    (DNS, compose-level port lockdown for qdrant/rsshub, reverse proxy
    + TLS, ufw, docker.sock decision). Returns control to `INSTALL.md`
    Phase 5 which carries a branch-C-specific external-verification
    block. Counterpart of operator-facing `docs/deployment/public.md`,
    but scoped to "no attacker comes in through a sembr-exposed
    surface" — generic VM hygiene (SSH hardening, OS patching,
    backups) stays out of scope and is surfaced as a Tell-user
    pointer.
  Closes the loop: deploy sembr with an agent, then have agents call
  into it.
- **Public-deployment guide** — `docs/deployment/public.md` with reverse-
  proxy samples (Caddy / nginx / Cloudflare Tunnel), `ufw` and SSH
  hardening steps, and a `nmap`-based verification checklist for users
  self-hosting on cloud VMs.
- **Logbus** — in-process log ring buffer with SSE fan-out powering the
  dashboard Logs tab without writing to disk.
- **Right-sized resource footprint** — measured baseline ~1 GiB across the
  three containers (api ~125 MiB / rsshub ~355 MiB / qdrant ~520 MiB at
  the default 53-source workload). Default `mem_limit`s (api 1.5G /
  qdrant 2G / rsshub 512M) leave ~4× headroom; documented scale-up paths
  for millions-of-vector workloads. Runs comfortably on 4 GB RAM hosts —
  homelab, Mac mini, NAS, $10 VPS.

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
