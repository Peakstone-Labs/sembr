# collector

> **Status**: pending review

RSS polling and article ingestion pipeline. Fetches feeds on per-feed APScheduler intervals, parses with feedparser, deduplicates via MD5 fingerprint, embeds, and upserts to Qdrant. Respects per-host rate limiting.

<!-- Review and fill in this page before opening the module to contributors. -->
