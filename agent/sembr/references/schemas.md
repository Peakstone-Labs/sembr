# Request body schemas

The bodies agents most often construct are `IntentCreate`, `FeedCreate`, `ExternalFireRequest`, and `ArchiveSearchRequest`. Fields not marked optional are required by the server.

## `IntentCreate`

Discriminated unions (`channels[].type`, `schedule.mode`) are where most agents trip up. Use the exact shapes below.

```jsonc
{
  "name": "openai-anthropic-releases",      // 1–100 chars; server enforces uniqueness
  "text": "OpenAI, Anthropic, and DeepMind product launches and benchmark releases. Exclude blog-only opinion pieces.",
  "sub_texts": [                            // optional; up to 3 multilingual phrasings
    {"language": "en", "text": "US Federal Reserve policy impact on emerging-market currencies."},
    {"language": "zh", "text": "美联储政策对新兴市场货币的影响。"}
  ],
  "threshold": 0.60,                        // 0.60–0.95; lower = more permissive
  "enabled": true,
  "channels": [                             // 1–10 entries. 1.0 supports only "email".
    {
      "type": "email",
      "to":  ["you@example.com"],           // 1–10 addresses
      "cc":  [],                            // optional
      "bcc": []                             // optional
    }
  ],
  "schedule": { /* pick ONE shape — see below */ },
  "feed_filter": null,                      // null = scan ALL feeds. {"ids":[1,3]} = subset. {"ids":[]} = pause (matches nothing).
  "timezone": "Asia/Shanghai",              // IANA tz; affects digest rendering AND cron firing wall-clock time
  "language": "zh",                         // digest output language; "en", "zh", etc.
  "system_template": "default",             // template name from GET /api/prompts/templates
  "instruction_template": "default"
}
```

### Schedule — cron mode

```jsonc
{
  "mode": "cron",
  "preset": "daily",                        // "daily" | "weekly" | "hourly"
  "hour": 9,                                // 0–23  (used by daily / weekly)
  "minute": 0,                              // 0–59  (used by all presets; hourly only honours minute)
  "weekday": "mon",                         // required ONLY when preset == "weekly"
  "lookback_seconds": 86400,                // 300–2_592_000 (5 min to 30 d)
  "skip_seen": true                         // dedupe against prior `match_seen` rows
}
```

### Schedule — event mode

Event-mode intents fire as articles arrive, not on a clock. The `/intents/{id}/fire` endpoints return **409** for event intents.

```jsonc
{
  "mode": "event",
  "trigger_count": 3,                       // fire after this many articles cross threshold
  "max_wait_seconds": 3600                  // even if trigger_count not reached, fire after this long
}
```

## `IntentUpdate`

Subset of `IntentCreate` — every field optional. Use `PUT /intents/{id}` to change one field at a time. **Special-case `text`:** changing it clears `match_seen` for this intent so the next scan can re-fire articles already seen. Tell the operator before mutating `text`.

## `FeedCreate`

```jsonc
{
  "name": "Reuters Top News",
  "url": "https://www.reuters.com/world/rss",   // for RSS: http(s):// URL. See below for newsapi / twitter
  "source_type": "rss",                          // "rss" | "newsapi" | "twitter"
  "config": {},                                  // source-type-specific knobs; {} = use defaults
  "poll_interval_minutes": 30,                   // 5–1440
  "tags": ["news", "finance"]                    // kebab-case, 0–10 tags
}
```

### NewsAPI.ai source

`url` is the source's host as NewsAPI labels it; `config.sourceUri` repeats it.

```jsonc
{
  "name": "NewsAPI: BBC",
  "url": "bbc.com",
  "source_type": "newsapi",
  "config": {"sourceUri": "bbc.com"},
  "poll_interval_minutes": 30,
  "tags": ["news", "newsapi"]
}
```

### Twitter source (via RSSHub sidecar)

Requires `TWITTER_AUTH_TOKEN` set in `.env` on the host. `url` is the screen name only — no `@`, no full URL.

```jsonc
{
  "name": "Elon Musk",
  "url": "elonmusk",
  "source_type": "twitter",
  "config": {"screen_name": "elonmusk"},
  "poll_interval_minutes": 30,
  "tags": ["twitter"]
}
```

## `ExternalFireRequest` (body for `POST /api/external/intents/{id}/fire`)

Every field optional — omitted fields fall back to the intent's stored values. **`extra="forbid"` → unknown fields are 422.** Do not invent extra fields; use only the four listed below.

```jsonc
{
  "lookback_seconds": 86400,                // 300–2_592_000
  "threshold": 0.70,                        // 0.20–0.95 (wider than IntentCreate's 0.60–0.95)
  "skip_seen": false,                       // false = ignore prior match_seen; useful for diagnostics
  "feed_ids": null                          // null = all feeds; [1,3] = subset
}
```

## `ArchiveSearchRequest` (body for `POST /api/archive/search`)

Every field is optional and unknown fields are rejected (`extra="forbid"`). A non-blank `query` selects semantic mode; omitting it selects newest-first filter mode.

```jsonc
{
  "query": "美联储降息路径",              // optional; ≤2000 chars. blank → filter mode
  "limit": 20,                            // 1–100; default 20
  "min_score": 0.50,                      // -1.0–1.0; semantic mode only
  "exclude_ids": ["point-id"],           // ≤1000 ids; semantic deepening/dedup

  "ingested_from_ts": 1780000000,         // inclusive Unix seconds
  "ingested_to_ts": 1785000000,
  "published_from_ts": 1780000000,        // missing/unparseable source dates do not match
  "published_to_ts": 1785000000,
  "feed_ids": [33, 37],                   // include any; [] is invalid—omit instead
  "exclude_feed_ids": [108],
  "title_contains": "Federal Reserve",   // blank is invalid
  "url_domains": ["reuters.com"],        // [] is invalid
  "min_body_len": 1000,                   // ≥0
  "matched_intent_ids": [29],             // intent ids captured at archive time; [] is invalid
  "langs": ["zh", "en"],                // [] is invalid

  "include_body": false,                  // default true; body_len remains present
  "cursor": {                             // filter mode only; pass next_cursor back verbatim
    "before_ts": 1784902487,
    "boundary_ids": ["point-id"]          // ≤500
  }
}
```

Mode consistency is strict:

- `min_score` without a semantic `query` → 422.
- `cursor` together with `query` → 422; semantic deepening uses `exclude_ids`.
- Empty include filters (`feed_ids`, `url_domains`, `matched_intent_ids`, `langs`) and blank `title_contains` → 422 because silently treating them as “no filter” could expose the whole archive.
- Empty exclusion lists are valid and mean no exclusion.

### `ArchiveSearchResponse`

```jsonc
{
  "mode": "semantic",                    // "semantic" | "filter"
  "hits": [
    {
      "id": "point-id",
      "score": 0.584,                     // null in filter mode
      "title": "…",
      "url": "https://…",
      "body": null,                       // null when include_body=false
      "published_at": "2026-07-20T12:00:00+00:00",
      "published_at_ts": 1784548800,
      "ingested_at_ts": 1784549000,
      "feed_id": 33,
      "feed_name": "Reuters",
      "url_domain": "reuters.com",
      "lang": "en",
      "body_len": 4210,
      "matched_intents": [29, 34],
      "embedding_model_version": "bge-m3_v1",
      "archived_at_ts": 1787141000
    }
  ],
  "warnings": [],
  "next_cursor": null                     // object only for a full filter-mode page
}
```

`feed_name` is best-effort. A null value means either the feed no longer exists or name resolution degraded; check `warnings` before deciding which.

### `ArchiveStatsResponse` (`GET /api/archive/stats`)

```jsonc
{
  "points_count": 12345,
  "earliest_ingested_at_ts": 1780000000,
  "latest_ingested_at_ts": 1785000000,
  "alias_ok": true                        // false=mismatch; null=check failed
}
```
