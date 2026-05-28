# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Documentation Paths

Internal development & strategy docs are kept in a **private sibling repository**, not in this repo.

> **Private doc IDs must not appear in source code.** Design decision IDs from `sembr-dev-docs/` (patterns: `Dxx`, `Rxx`, `DDxx`, `SCxx`, `P0-x`) are for internal tracking only. Never write them into `.py` comments, docstrings, or public `.md` files — CI will reject the PR (`No private dev-doc references` job). Record traceability in `implementation.md` inside `sembr-dev-docs/` instead.

- **Dev docs root**: `../sembr-dev-docs/development/` (relative to project root)
- **Research root**: `../sembr-dev-docs/research/`

**Per-feature convention** — every feature gets its own kebab-case folder under the dev docs root; all agents (clarify / architect / dev / qa / review / retro) collaborating on the feature write into the same folder:

```text
../sembr-dev-docs/development/<feature-name>/
  requirements.md     ← clarify
  design.md           ← architect
  implementation.md   ← dev
  progress.md         ← shared progress tracker
  test_report.md      ← qa
  review.md           ← review
  retro.md            ← retro
```

`<feature-name>` is **kebab-case** (e.g. `rss-collector`, `intent-crud`).

**Public docs (MkDocs site)** live at `/docs/` in this repo and ARE tracked in git. They are the open-source-facing documentation rendered to GitHub Pages via `.github/workflows/docs.yml`. Internal feature docs go in the private sibling repo above; user-facing module / API / getting-started docs go in `/docs/`.

## Project Overview

**sembr** (semantic + embrace) is an open-source self-hosted **intent radar** built on "Reverse RAG", from Peakstone-Labs. Unlike traditional RAG (where users query documents), sembr stores user intent vectors and continuously scans incoming articles across RSS / NewsAPI / Twitter for semantic matches, then pushes LLM-analyzed digests via email (with Telegram / Discord / Slack channels scaffolded by the marker-ABC plugin point for post-1.0 work).

The core data flow: RSS feeds → BGE-M3 embeddings → Qdrant → intent vector ANN search → LLM summary → push notification.

## Tech Stack

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.12.x | Not 3.11 |
| FastAPI | 0.115.14 | Pydantic v2 native |
| APScheduler | **3.11.2** | NOT 4.0 — API rewritten, data incompatible |
| Qdrant Server | **1.17.1** | `qdrant/qdrant:v1.17.1` docker image |
| qdrant-client | 1.17.1 | Use `AsyncQdrantClient` |
| aiosqlite | 0.20.x | SQLite WAL mode required |
| httpx | >=0.27,<0.28 | Async HTTP client for SiliconFlow embeddings API |
| Embedding model | bge-m3 via SiliconFlow API | 1024-dim, 8192 token ctx, OpenAI-compatible `/v1/embeddings` |
| LLM | DeepSeek-V4-Flash via SiliconFlow (default) | OpenAI-compatible `/v1/chat/completions`; any same-protocol endpoint works. Local backends (mlx-lm, Ollama) post-1.0 |

## Development Environment

| Machine | Role | Notes |
| ------- | ---- | ----- |
| Windows dev machine | Code editing, static tests | No Docker runtime for sembr; `py_compile` / `asyncio` tests run here |
| Mac Mini (same LAN, `ssh mac-mini`) | Docker runtime, E2E validation | M4 16GB — project directory: `~/sembr` |

**uv:** not globally installed on Windows. Run `pip install uv` when `uv lock` / `uv sync` is needed. Mac Mini should have uv installed natively.

**Static tests** (run on Windows Python / Anaconda — no runtime deps needed):

```bash
python -m py_compile sembr/**/*.py
python -c "import sembr, sembr.api, sembr.collector, sembr.embedder, sembr.vector_store, sembr.matcher, sembr.summarizer, sembr.notifier, sembr.db; print('ok')"
pytest tests/ -v
```

**E2E validation** (run on Mac Mini after `git pull`):

```bash
cp .env.example .env
docker compose up --build          # AC#1 — one-click start
curl -i http://localhost:8000/health  # AC#2 — /health 200
docker compose exec api python -c \
  "import sqlite3; c=sqlite3.connect('/app/data/sembr.db'); print(c.execute('PRAGMA journal_mode').fetchone())"  # AC#4 — WAL
docker compose exec api python -c \
  "import sembr.api, sembr.collector, sembr.embedder, sembr.vector_store, sembr.matcher, sembr.summarizer, sembr.notifier, sembr.db; print('ok')"  # AC#6
```

## Commands

```bash
# Start all services (on Mac Mini)
docker compose up --build

# Start with rebuild
docker-compose up --build

# Run API server only (development)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Run tests
pytest tests/

# Run a single test file
pytest tests/test_matcher.py -v

# Run tests with coverage
pytest tests/ --cov=sembr --cov-report=term-missing

# Lint
ruff check .
ruff format .
```

## Architecture

### Module Structure

```
sembr/
├── main.py                  # FastAPI app + lifespan (APScheduler integration)
├── config.py                # pydantic-settings config (5-level priority chain)
├── models.py                # Pydantic domain models
├── api/
│   ├── feeds.py             # POST/GET/DELETE /feeds
│   ├── intents.py           # CRUD /intents
│   └── health.py            # GET /health
├── collector/
│   ├── base.py              # BaseSource(ABC): fetch(since), config_schema()
│   ├── rss.py               # feedparser-based RSSSource
│   └── scheduler.py         # SOURCE_REGISTRY, APScheduler per-feed jobs, host limiter
├── embedder/
│   ├── openai_compat.py     # SiliconFlowEmbedder — httpx async client for /v1/embeddings
│   └── factory.py           # build_embedder(settings) → BaseEmbedder
├── vector_store/
│   ├── qdrant.py            # AsyncQdrantClient wrapper, extract_point_vector helper
│   ├── intents.py           # ALIAS_NAME = "intents_current", upsert/delete/payload helpers
│   └── news.py              # ALIAS_NAME = "news_current", quantized news collection
├── matcher/
│   ├── scan.py              # cron-mode scan_once via qdrant query_points
│   ├── jobs.py              # register/unregister APScheduler jobs per intent
│   ├── event_match.py       # event-mode in-process scoring
│   ├── event_buffer.py      # event_pending drain logic
│   └── event_cache.py       # in-process event-mode intent vector cache
├── summarizer/
│   ├── pipeline.py          # SummaryPipeline: build prompts, water-fill bodies, call LLM
│   ├── llm/api.py           # APIBackend (OpenAI-compatible /v1/chat/completions)
│   └── templates.py         # Jinja2 + strict-placeholder render, template_path helper
├── notifier/
│   ├── base.py              # BaseChannel — marker ABC (no abstract methods)
│   └── email.py             # SMTP via asyncio.to_thread, multipart/related digest
├── logbus/                  # in-process log ring buffer + SSE fan-out for the dashboard
└── db/
    └── sqlite.py            # aiosqlite, WAL init, transaction() context manager
```

### Key Architectural Decisions

**Dual-collection Qdrant design**: `intents_current` alias holds pre-computed user intent vectors; `news_current` alias holds article vectors. The matcher uses `query_points(query=vector, score_threshold=..., query_filter=...)` (qdrant-client 1.10+ — `search()` and `search_batch()` were removed) per intent, filtered by `ingested_at_ts > lookback_cutoff` and an optional per-intent `feed_id` whitelist. Both alias names live as `ALIAS_NAME` constants in `vector_store.intents` / `vector_store.news` — never hardcode the strings.

**Reverse RAG**: Intent vectors are computed once at creation time. Scanning is O(intents × new_articles) via per-intent `query_points`, not O(queries). Default similarity threshold is 0.75 (user-configurable 0.60–0.95 per intent).

**LLM backend**: 1.0 ships only `APIBackend` (any OpenAI-compatible `/v1/chat/completions` endpoint, default DeepSeek-V4-Flash on SiliconFlow). Local backends (mlx-lm, Ollama) are post-1.0 work — implement `BaseLLMBackend` and add to `factory.build_llm_backend`. `BaseLLMBackend.max_prompt_chars` is the contract `SummaryPipeline` water-fills against.

**Embedding engine**: `SiliconFlowEmbedder` calls the SiliconFlow `/v1/embeddings` API via `httpx.AsyncClient` — no local model, no thread pool. Batch size = 32 (SiliconFlow single-request limit). The `embedder.load()` coroutine runs a startup probe; `/health` returns 503 until the probe succeeds. Never block the FastAPI event loop.

**SQLite WAL**: Must initialize with `PRAGMA journal_mode=WAL; synchronous=NORMAL; cache_size=-64000; busy_timeout=5000`. WAL ensures readers never block writers. There is no `notification_log` retry/DLQ table in 1.0 — failed notifier sends are logged and dropped.

**APScheduler integration**: Use `AsyncIOScheduler` integrated via FastAPI `lifespan` context manager. Set `coalesce=True` on all jobs to prevent backlog on recovery. Per-intent matcher jobs follow the intent's own schedule (cron preset or event); RSS polling defaults to every 30 minutes per feed.

**Deduplication**: Two-layer — (1) `MD5(url + title)` fingerprint for exact dedup at ingest; (2) per-intent `match_seen` rows so a cron-mode rescan over the same lookback window doesn't re-fire the same article. `match_seen` cascades on intent delete; PUT with text change clears it for the affected intent.

**Source registration**: 1.0 uses a hardcoded `SOURCE_REGISTRY: dict[str, type[BaseSource]]` in `collector.scheduler`. The dashboard reads it for the create-feed form, so a new source type appears once it's added to the dict. Entry-points-based plugin discovery is post-1.0 work.

**Channel registration**: `BaseChannel` is a marker ABC (no abstract methods — per-channel `send()` signatures legitimately diverge). Adding a channel = (a) new `XConfig: BaseModel` with a unique `Literal["x"]` discriminator, (b) add to `Intent.channels` discriminated union, (c) new `isinstance` arm in the dispatcher in `main.py`.

### Embedding Model Versioning

Collections are named `news_{model}_{version}` (e.g., `news_bge-m3_v1`). The application accesses via alias `news_current`. On model upgrade: create new collection in parallel, re-embed in background via low-priority APScheduler job, then atomically switch alias. Every document payload and intent vector must include `embedding_model_version` field.

### Docker Compose memory limits

```yaml
api:    mem_limit 1.5G, reservation 512M
qdrant: mem_limit 2G,   reservation 1G
rsshub: mem_limit 512M
```

Right-sized 2026-05-13 from live Mac mini measurement (api ~125 MiB, rsshub ~355 MiB, qdrant ~520 MiB; total ~1 GB at the default 53-source workload). Each limit leaves ~4x headroom for bursts. Raise `qdrant.mem_limit` to 4G+ at scale (millions of articles); raise `api.mem_limit` to 3G if you run tens of intents with concurrent fire bursts.

Qdrant stores quantized vectors in RAM (`always_ram=True`) and raw vectors on disk (`on_disk=True`) using Scalar int8 quantization. 10M vectors at 1024-dim ≈ 600MB RAM.

## Configuration

Uses `pydantic-settings` with four-level priority (highest wins): shell env vars → `.env` file → `sembr.yaml` file (CWD) → built-in defaults. There is no `secrets_dir=` today — Docker secrets land via shell env vars on the container side. Sensitive values (API keys) belong in env vars, never in committed files.

## 1.0 Acceptance Criterion

New user goes from `git clone` to receiving the first matched email digest within 15 minutes: clone → `cp .env.example .env` → set `EMBEDDER_API_KEY` and SMTP creds → `docker compose up --build` → POST an intent with `"channels": [{"type":"email","to":["..."]}]` → receive digest on the intent's schedule.

## Roadmap Context

- **1.0** (current): RSS + BGE-M3 + Qdrant + intent CRUD (cron + event modes) + email digest + monitoring dashboard + runtime settings editor
- **post-1.0**: additional source plugins (Reddit, HN, etc.), additional channels (Telegram / Discord / Slack), local LLM backend, entry-points-based plugin discovery, `notification_log` retry/DLQ pipeline, multi-worker / multi-process deployment story

## Project Identity

- GitHub: `Peakstone-Labs/sembr`
- PyPI: `sembr`
- License: Apache-2.0
- Positioning: "Intent-Driven Monitoring" / "Reverse RAG" — define this category vocabulary in docs and READMEs
- Target users: quant analysts (P0), AI developers/DevOps (P1), content ops (P1)
