# vector_store

> **Status**: pending review

Async Qdrant wrapper. Maintains two collections: `intents` (pre-computed intent vectors) and `news_current` (article vectors with `ingested_at_ts` payload index). Handles collection creation, alias management, upsert, and ANN search.

<!-- Review and fill in this page before opening the module to contributors. -->
