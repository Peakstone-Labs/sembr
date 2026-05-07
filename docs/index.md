# sembr

> Intent-driven news monitoring via Reverse RAG.

**sembr** lets you write a natural-language intent once — *"monitor Fed policy impact on emerging-market currencies"* — and receive matching news as a digest email. No keyword lists, no hand-tuned filters. Telegram / Discord / Slack channels and a local LLM backend are scaffolded by the plugin seams and on the post-1.0 roadmap.

## How it works

Traditional RAG: user query → search documents → answer.

**Reverse RAG (sembr)**: pre-stored intent vector → continuously scan incoming articles → push semantic matches.

You describe what you care about once. sembr does the watching.

```
RSS Feeds
    │
    ▼
BGE-M3 Embeddings (SiliconFlow API)
    │
    ▼
Qdrant Vector Store
    │  ◄── Intent vectors (stored at creation time)
    ▼
Semantic Matcher (Qdrant query_points per intent)
    │
    ▼
LLM Summary (OpenAI-compatible chat completions)
    │
    ▼
Email Digest (SMTP)
```

Each intent picks its own schedule — cron-mode (`hourly` / `daily` / `weekly` preset with a per-intent timezone) or event-mode (fire after N matching articles, or after T seconds since the first buffered match).

## Quick links

- [Getting Started](getting-started.md) — Docker Compose in 3 steps
- [Configuration](configuration.md) — every setting with its real default
- [Architecture](architecture.md) — design decisions explained
- [Modules](modules/index.md) — per-module interface reference

## Status

**1.0** — first stable release.

- RSS ingestion + BGE-M3 embeddings via SiliconFlow API + Qdrant
- Intent CRUD via REST API, cron-mode and event-mode schedules
- LLM-generated summaries via any OpenAI-compatible chat-completions endpoint
- Email digest delivery via SMTP, rendered in each intent's own timezone with matcher-score badges per source
- Read-only monitoring dashboard with live log SSE
- Runtime settings editor — write the host `.env` and recreate the affected containers in place

## License

[Apache-2.0](https://github.com/Peakstone-Labs/sembr/blob/main/LICENSE) · Built by [Peakstone Labs](https://github.com/Peakstone-Labs)
