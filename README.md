<p align="center">
  <img src="assets/brand/logo-lockup.png" alt="sembr" width="320">
</p>

<p align="center">
  <b>Reverse RAG.</b><br>
  <i>Always-on retrieval service for any input stream.</i>
</p>

<p align="center">
  <a href="https://github.com/Peakstone-Labs/sembr/actions/workflows/ci.yml"><img src="https://github.com/Peakstone-Labs/sembr/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="Dockerfile"><img src="https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white" alt="Docker"></a>
</p>

<p align="center">
  <a href="README.zh-CN.md">中文</a> ·
  <a href="https://peakstone-labs.github.io/sembr">Documentation</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="docs/deployment/public.md">Public deployment</a> ·
  <a href="https://github.com/Peakstone-Labs/sembr/discussions">Discussions</a>
</p>

---

**sembr** is a self-hosted news-monitoring agent. Describe what you care about in plain English — _"monitor Fed policy impact on emerging-market currencies"_ — and sembr keeps watching for you: continuously pulling new articles across RSS feeds, [NewsAPI.ai](https://newsapi.ai), and Twitter, vector-matching them to your intent, and emailing an LLM-written digest on whatever schedule you set.

You write the intent once. sembr does the retrieval forever.

<p align="center">
  <img src="assets/brand/hero.png" alt="sembr — Reverse RAG" width="720">
</p>

<!-- TODO: add product UI strip (intent editor / dashboard / digest email) once captured -->

## Why sembr

- **Semantic, not keyword.** Your intent is an embedding, not an `OR`-list. *"EM currency contagion"* matches *"Turkish lira plunges as Fed eyes another hike"* with zero shared words.
- **Bilingual out of the box.** [BGE-M3](https://huggingface.co/BAAI/bge-m3) was picked specifically for CJK + English mixed content. Bloomberg, SCMP, 财联社, 华尔街见闻, Nature, 36氪 can all sit under one intent and the matcher doesn't care which language an article is in.
- **Free embeddings, pennies per digest.** The default embedder (BGE-M3 on [SiliconFlow](https://siliconflow.cn)) is free at any volume. The default LLM (DeepSeek-V4-Flash) is paid but extremely cheap — and its 1 M-token context window means one digest can chew through a hundred long-form articles for well under a cent. Same OpenAI-compatible protocol means you can swap to OpenAI / Together / Groq / Ollama / mlx-lm any time.
- **Your watchlist never leaves your box.** What you're monitoring is itself signal — sensitive financial or journalistic intents leak research direction to whichever vendor sees them. sembr runs on your hardware (homelab / Mac mini / NAS / a $5 VPS); only outbound calls are to the embedder and LLM endpoints you choose.
- **Cron or event.** Per-intent schedule: a fixed digest time (*"every weekday 09:00 in Asia/Shanghai"*) or event-mode (*"fire the moment 3 matches accumulate, but at most every 30 min"*).
- **Pluggable everywhere.** Source / channel / embedder / LLM are all ABC seams. Telegram, Discord, Slack channels, local LLM backends (mlx-lm, Ollama), and more source plugins (Reddit, HN, Mastodon) are scaffolded for post-1.0.
- **Agent-callable.** `POST /api/external/intents/{id}/fire` returns matches plus an LLM summary in one synchronous JSON round-trip — drop sembr into any agent stack you run (Hermes, OpenClaw, LangGraph, your own) and let the orchestrator decide when to look at the world. Per-call overrides for lookback, threshold, and feed scope; no notification side-effects.

## How "Reverse RAG" works

> *Attention is all you need.* — Vaswani et al., 2017
>
> *AI is your attention.* — sembr

Classic RAG: user types a query → app retrieves matching documents → LLM answers.

**Reverse RAG (sembr):** user defines an intent → sembr embeds it once → every new article runs against every standing intent vector → matches get summarized and pushed.

The flip is small but its implications are big. Queries become first-class entities you can name, edit, schedule, and version. Retrieval becomes a long-running job, not a request-response round-trip. *"Answer quality"* becomes *"how relevant were the last 10 things I was told about."*

→ Full architecture write-up: [docs/architecture.md](docs/architecture.md)

## Quickstart

**Got an AI coding agent on this machine?** (Claude Code / Cursor / Cline / Aider / Continue / Roo) — paste it the URL of [`INSTALL.md`](INSTALL.md). It'll handle hardware checks, dependency install, parallel Docker pulls, and `.env` setup; you'll only be asked for API keys.

**Manual install** (everything below, ~15 min). Requires Docker + Docker Compose. First run pulls Qdrant + RSSHub and builds the API image (Python 3.12 base + Docker CLI + pip wheels) — **about 1 GB total network download, 10–15 minutes on a typical home connection**. `/health` returns `503` until the embedder probe completes.

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
cp .env.example .env                 # 1. seed config
# open .env, set EMBEDDER_API_KEY (free key at https://siliconflow.cn)
docker compose up --build            # 2. start everything

# in another shell, 1–2 minutes later:
curl -i http://localhost:8000/health         # 200 once embedder probe completes
open http://localhost:8000/dashboard          # web UI
```

Out of the box: 53 pre-loaded sources across RSS / NewsAPI / Twitter (EN + CN), a live dashboard, and a working `/intents` API. Create your first intent:

```bash
curl -X POST http://localhost:8000/intents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "fed-emerging-markets",
    "text": "Fed policy impact on emerging-market currencies and capital flows",
    "timezone": "America/New_York",
    "schedule": {"mode": "cron", "preset": "daily", "hour": 8, "minute": 0},
    "channels": [{"type": "email", "to": ["you@example.com"]}]
  }'
```

Next digest fires on schedule. Done.

→ Step-by-step walkthrough: [docs/getting-started.md](docs/getting-started.md)
→ Putting sembr on a public IP? Read [docs/deployment/public.md](docs/deployment/public.md) first — TL;DR keep the default `127.0.0.1` bind, put sembr behind a reverse proxy with TLS, and set a strong `DASHBOARD_TOKEN`.

## What's in the box

**53 pre-loaded sources across three source types** — curated for substantive body text or information-dense headlines:

| Source type | Pre-loaded | Examples |
| --- | --- | --- |
| RSS feeds | 22 | The Guardian, SCMP, NPR, Washington Post, Bloomberg Markets, 华尔街见闻, 第一财经, 36氪, 虎嗅, 财联社电报, 澎湃, 国家统计局, Nature ×3, HelloGitHub |
| Twitter | 1 | Elon Musk — extend with your own users / keyword searches via a `TWITTER_AUTH_TOKEN` cookie |
| [NewsAPI.ai](https://newsapi.ai) aggregator | 30 | Reuters, BBC, NYT, WSJ, FT, Economist, Bloomberg, The Atlantic, NPR, TechCrunch, Wired, Ars Technica, Vox, … |

RSS routes that need a JS-rendering origin (most CN sources, Twitter) go through the bundled **[RSSHub](https://rsshub.app)** sidecar — no extra setup. NewsAPI.ai's free signup token covers roughly 30 days of normal polling; get one at [newsapi.ai](https://newsapi.ai) and drop it into `.env`. Full per-feed list: [docs/getting-started.md](docs/getting-started.md).

- **BGE-M3 embeddings** via SiliconFlow (free), or any OpenAI-compatible `/v1/embeddings` endpoint
- **[Qdrant](https://qdrant.tech) vector store** with scalar int8 quantization (10M vectors fit in ~600 MB RAM)
- **LLM digest generation** via any OpenAI-compatible `/v1/chat/completions` — defaults to DeepSeek-V4-Flash on SiliconFlow
- **Email delivery** (SMTP, multipart/related, per-intent timezone, matcher-score badges)
- **Monitoring dashboard**: live feed health, embedder latency, container CPU / mem / uptime, Qdrant article browser with date / source / title filters, log SSE, one-click restart
- **Runtime settings editor** that writes the host `.env` and recreates the affected containers in place — you can do everything from the UI
- **Custom prompt templates** — system + instruction, with strict-placeholder validation and dashboard CRUD

→ Module-by-module deep dives: [docs/modules/](docs/modules/index.md)

## Configuration

`pydantic-settings` with a four-level precedence chain (highest wins):

1. Shell env vars
2. `.env` file (project root)
3. `sembr.yaml` (project root)
4. Built-in defaults

Sensitive values (`EMBEDDER_API_KEY`, `LLM_API_KEY`, `DASHBOARD_TOKEN`, SMTP creds) belong in env vars or a properly-permissioned `.env` — never in committed files. Full surface: [docs/configuration.md](docs/configuration.md).

> ⚠️ **Set `DASHBOARD_TOKEN` whenever the host is reachable beyond `localhost`.** Without it, `/api/dashboard/*` and the settings editor are unauthenticated. The Settings editor also bind-mounts the host docker socket so it can recreate containers — that's a deliberate single-tenant trade-off (same model as Watchtower / Portainer); anyone with API access is effectively docker-root on the host. Don't run sembr on a multi-tenant host without accepting that. See [docs/deployment/public.md](docs/deployment/public.md) for the full hardening checklist.

## Tech stack

Python 3.12 · FastAPI 0.115 · Pydantic v2 · APScheduler 3.11 · aiosqlite (WAL) · Qdrant 1.17 · httpx · BGE-M3 · DeepSeek-V4-Flash · Apache-2.0

## Status

**v1.0** — first stable release. Ships RSS ingestion, BGE-M3 embeddings, Qdrant dual-collection, intent CRUD (cron + event), LLM-summarized digests, email channel, monitoring dashboard, runtime settings editor, and a hardened public-deployment guide.

**Post-1.0:** Telegram / Discord / Slack channels, local LLM backends (mlx-lm, Ollama), Reddit / HN / Mastodon source plugins, entry-points plugin discovery, notification retry / DLQ, multi-worker deployment.

→ Versioning policy and changelog: [CHANGELOG.md](CHANGELOG.md)

## Alternatives, and why sembr exists

The closest things in the market today:

- **Feedly Pro+ "AI Feeds"** ($99 / yr) — the closest semantic competitor. 15 languages, but non-English articles are translation-truncated at ~1,600 chars, your watchlist lives on Feedly's servers, and the AI tier is gated above the entry-level plan.
- **Inoreader Pro** ($90 / yr) — rules + keyword filters with AI summaries on a monthly token budget. No vector-matching of standing intents.
- **Brand24 / Mention** ($199+ / mo) — enterprise mention monitoring, keyword-driven, hosted only, priced per analyst.
- **Bloomberg Terminal** (~$32k / yr / seat) — gold standard for institutional desks; irrelevant to the long tail.
- **FreshRSS / miniflux** — self-hosted RSS readers you may already run. No semantic matching, no LLM digest, no intent concept.
- **Google Alerts** — free but keyword-only and famously weak on Chinese.

If you're an institution with budget, run Bloomberg or Brand24. If you're happy with a hosted plan and your watchlist isn't sensitive, Feedly Pro+ is great. sembr is for the slice where you want to (a) write watchlist briefs in natural language, (b) have them matched semantically across mixed-language feeds, (c) get an LLM digest on a schedule you control, and (d) pay close to $0 while owning all the data. As far as we can tell, nothing else sits at the intersection of all four.

## Built by

[Peakstone Labs](https://github.com/Peakstone-Labs) — AI-native quantitative research. sembr started as the news side of an internal alpha-research pipeline; opening it up makes it useful to a much wider set of people watching the same world we are.

If you have feedback, found a bug, or want a source / channel plugin: [Discussions](https://github.com/Peakstone-Labs/sembr/discussions) for ideas and questions, [Issues](https://github.com/Peakstone-Labs/sembr/issues) for bugs and concrete feature requests, [SECURITY.md](SECURITY.md) for vulnerability reports. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE). © 2025–2026 Peakstone Labs and sembr contributors.
