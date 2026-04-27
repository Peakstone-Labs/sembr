# sembr

> Semantic news monitoring via reverse RAG.

**sembr** lets you write a natural-language intent once — _"monitor Fed policy impact on emerging-market currencies"_ — and receive matching news pushed to Telegram, Discord, or Slack as it arrives. No keyword lists, no hand-tuned filters.

## How it differs from traditional RAG

Traditional RAG: user query → search documents → answer.
**Reverse RAG (sembr)**: pre-stored intent vector → continuously scan incoming articles → push semantic matches.

You describe what you care about once. sembr does the watching.

## Status

🚧 Pre-release — under active development. The 0.1.0 MVP targets:

- RSS ingestion + BGE-M3 embeddings + Qdrant vector store
- Intent CRUD via REST API
- 5-minute scheduled semantic matching with configurable threshold
- LLM-generated summaries (OpenAI / DeepSeek / local)
- Telegram push delivery

## License

Apache-2.0

## Built by

[Peakstone Labs](https://github.com/Peakstone-Labs) — AI-native quantitative research.
