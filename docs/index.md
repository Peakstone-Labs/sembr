# sembr

> Intent-driven news monitoring via Reverse RAG.

**sembr** lets you write a natural-language intent once — *"monitor Fed policy impact on emerging-market currencies"* — and receive matching news pushed to Telegram, Discord, or Slack as it arrives. No keyword lists, no hand-tuned filters.

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
Semantic Matcher (ANN search, every 5 min)
    │
    ▼
LLM Summary (OpenAI / DeepSeek / local)
    │
    ▼
Push Notification (Telegram / Discord / Slack)
```

## Quick links

- [Getting Started](getting-started.md) — Docker Compose in 3 steps
- [Configuration](configuration.md) — all settings with defaults
- [Architecture](architecture.md) — design decisions explained
- [Modules](modules/index.md) — per-module interface reference

## Status

Pre-release — 0.1.0 MVP under active development.

- RSS ingestion + BGE-M3 embeddings via SiliconFlow API + Qdrant
- Intent CRUD via REST API
- 5-minute scheduled semantic matching
- LLM-generated summaries
- Telegram push delivery

## License

[Apache-2.0](https://github.com/Peakstone-Labs/sembr/blob/main/LICENSE) · Built by [Peakstone Labs](https://github.com/Peakstone-Labs)
