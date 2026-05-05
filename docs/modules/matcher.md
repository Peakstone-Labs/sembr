# matcher

> **Status**: pending review

Scheduled ANN search job. Every 5 minutes, queries Qdrant for each active intent using `search_batch`, applies `ingested_at_ts` recency filter, deduplicates via match-seen table, and emits match events to the summarizer pipeline.

<!-- Review and fill in this page before opening the module to contributors. -->
