# Modules

Each page documents one module's public interface, upstream dependencies, downstream consumers, and known behavioral constraints.

Modules are listed in dependency order (bottom of the stack first):

| Module | Responsibility |
|--------|---------------|
| [db](db.md) | SQLite persistence — feeds, articles, intents, notification log |
| [embedder](embedder.md) | BGE-M3 text → vector via SiliconFlow API |
| [vector_store](vector-store.md) | Qdrant dual-collection read/write |
| [collector](collector.md) | RSS polling, article ingestion pipeline |
| [matcher](matcher.md) | Scheduled ANN search, match event emission |
| [summarizer](summarizer.md) | LLM summary generation from matched article clusters |
| [notifier](notifier.md) | Push delivery (Telegram / Discord / Slack / email) |
| [logbus](logbus.md) | In-process structured log event bus (SSE fan-out) |
| [api](api.md) | FastAPI REST endpoints |
| [dashboard](dashboard.md) | Read-model aggregation and monitoring UI |
