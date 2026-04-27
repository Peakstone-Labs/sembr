# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Documentation Paths

Internal development & strategy docs are kept in a **private sibling repository**, not in this repo.

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

`<feature-name>` is **kebab-case** (e.g. `rss-collector`, `intent-crud`). The main repo's `.gitignore` excludes `/docs/` defensively; do not create a `docs/` directory in this repository.

## Project Overview

**sembr** (semantic + embrace) is an open-source "Reverse RAG" news monitoring tool built by Peakstone-Labs. Unlike traditional RAG (where users query documents), sembr stores user intent vectors and continuously scans incoming news vectors for semantic matches, then pushes summaries via Telegram/Discord/Slack.

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
| LLM (local) | Qwen3-4B-4bit via mlx-lm | ~2.5GB, ~80 tok/s on M4 |
| LLM (API) | DeepSeek / OpenAI | Via abstract factory |

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
│   ├── base.py              # BaseSource(ABC): fetch(since), health(), config_schema()
│   ├── rss.py               # feedparser-based RSSSource
│   └── http.py              # httpx-based HTTPSource
├── embedder/
│   ├── openai_compat.py     # SiliconFlowEmbedder — httpx async client for /v1/embeddings
│   └── factory.py           # build_embedder(settings) → BaseEmbedder
├── vector_store/
│   └── qdrant.py            # AsyncQdrantClient wrapper; dual-collection design
├── matcher/
│   └── scheduler.py         # APScheduler job: search_batch + payload filter
├── summarizer/
│   ├── factory.py           # LLMFactory: local (mlx-lm) or API backend
│   ├── base.py              # BaseLLMBackend(ABC): summarize(), health()
│   └── templates/           # Jinja2 templates per language/channel
├── notifier/
│   ├── base.py              # BaseChannel(ABC): send(), health(), split_message()
│   ├── telegram.py          # aiogram 3.x, HTML parse_mode, auto-split at 4096 chars
│   ├── discord.py           # Webhook + Embed format
│   ├── slack.py             # Incoming Webhook + Block Kit
│   └── email.py             # smtplib fallback
└── db/
    └── sqlite.py            # aiosqlite, WAL init, notification_log state machine
```

### Key Architectural Decisions

**Dual-collection Qdrant design**: `intents` collection stores pre-computed user intent vectors; `news` collection stores article vectors. The matcher uses `search_batch()` with all active intent vectors querying the news collection, filtered by `published_at > last_scan_time`. Use Qdrant's `lookup_from` to reference intent vectors by point ID without re-embedding.

**Reverse RAG**: Intent vectors are computed once at creation time. Scanning is O(intents × new_articles) via `search_batch`, not O(queries). Default similarity threshold is 0.75 (user-configurable 0.60–0.95 per intent).

**LLM Abstract Factory**: `LLMFactory.create(config)` returns either `OllamaBackend` or `APIBackend` implementing `BaseLLMBackend`. On 16GB M4: use mlx-lm directly (Ollama 0.19 requires 32GB+ for MLX backend). The factory auto-detects available memory and selects the appropriate path.

**Embedding engine**: `SiliconFlowEmbedder` calls the SiliconFlow `/v1/embeddings` API via `httpx.AsyncClient` — no local model, no thread pool. Batch size = 32 (SiliconFlow single-request limit). The `embedder.load()` coroutine runs a startup probe; `/health` returns 503 until the probe succeeds. Never block the FastAPI event loop.

**SQLite WAL**: Must initialize with `PRAGMA journal_mode=WAL; synchronous=NORMAL; cache_size=-64000; busy_timeout=5000`. WAL ensures readers never block writers. The `notification_log` table tracks push state: `pending→sent→failed→dead`.

**APScheduler integration**: Use `AsyncIOScheduler` integrated via FastAPI `lifespan` context manager. Set `coalesce=True` on all jobs to prevent backlog on recovery. Matcher runs every 5 minutes; RSS polling defaults to every 30 minutes.

**Deduplication**: Two-layer — (1) `MD5(url + title)` fingerprint for exact dedup; (2) semantic dedup within same intent: merge if score delta < 0.05 and title similarity > 0.9.

**Source/Channel plugins**: Register via `pyproject.toml` entry_points (`sembr.sources`, `sembr.channels`). `config_schema()` JSON Schema is auto-rendered as a UI form.

### Embedding Model Versioning

Collections are named `news_{model}_{version}` (e.g., `news_bge-m3_v1`). The application accesses via alias `news_current`. On model upgrade: create new collection in parallel, re-embed in background via low-priority APScheduler job, then atomically switch alias. Every document payload and intent vector must include `embedding_model_version` field.

### Docker Compose (16GB M4 limits)

```yaml
api:    memory limit 3G
qdrant: memory limit 4G, reservation 2G
```

Qdrant stores quantized vectors in RAM (`always_ram=True`) and raw vectors on disk (`on_disk=True`) using Scalar int8 quantization. 10M vectors at 1024-dim ≈ 600MB RAM.

## Configuration

Uses `pydantic-settings` with priority: defaults → `sembr.yaml` file → `.env` → env vars → Docker secrets → runtime API overrides. Sensitive values (API keys, bot tokens) via env vars or Docker secrets, never in committed files.

## MVP Acceptance Criterion

New user should go from `git clone` to receiving first matched push notification within 15 minutes. Benchmark this path during development: clone → `docker-compose up` → configure Telegram token → create intent via API → receive push.

## Roadmap Context

- **0.1.0 (MVP)**: RSS + bge-m3 + Qdrant + intent CRUD + Telegram push + Docker Compose
- **0.2.0**: Plugin sources (Reddit, HN), Discord/Slack channels, Ollama LLM, per-intent threshold tuning
- **1.0.0**: Web UI, multi-embedding model support with zero-downtime alias switching, monitoring dashboard, 30-day stable operation

## Project Identity

- GitHub: `Peakstone-Labs/sembr`
- PyPI: `sembr`
- License: Apache-2.0
- Positioning: "Intent-Driven Monitoring" / "Reverse RAG" — define this category vocabulary in docs and READMEs
- Target users: quant analysts (P0), AI developers/DevOps (P1), content ops (P1)
