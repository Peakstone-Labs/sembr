# Endpoint surface

Authoritative schema: `GET /openapi.json`. This page is a curated subset tracking sembr **1.0**.

## Sanity / discovery (read-only)

| Method & path | Purpose |
| --- | --- |
| `GET /health` | Is the stack up? `{"status":"ok"}` ⇒ yes. `503` ⇒ embedder probe still warming — retry in 30 s. **No auth.** |
| `GET /intents` | List every intent. |
| `GET /intents/{id}` | Full record for one intent. |
| `GET /feeds` | List every feed (RSS / NewsAPI / Twitter). |

## News search (read-only)

One endpoint over the whole news timeline — recent and historical alike. It never sends notifications and never writes `match_seen`.

| Method & path | Purpose |
| --- | --- |
| `POST /api/news/search` | Semantic retrieval when the JSON body contains a non-blank `query`; otherwise newest-first filtered listing. Request/response shapes: `schemas.md`. |
| `GET /api/dashboard/maintenance/qdrant_stats` | Operator view: per-store point counts, ingestion time ranges, alias health, backfill queue depth. |

Both require `X-Dashboard-Token` when authentication is configured.

Storage is internally split between a live store and a permanent archive, and the search endpoint hides that completely: it queries both and returns one ranked list. There is no `scope` parameter and no marker on a hit. To restrict by age, use the time-range filters.

Retrieval-mode rules:

- **Semantic mode**: send `query`; paginate/deepen by sending prior point ids in `exclude_ids`. `min_score` is valid only in this mode. There is no cursor.
- **Filter mode**: omit `query`; a full page returns `next_cursor`. Pass that object back verbatim. Results are ordered newest-first by `ingested_at_ts`.
- Filters work in both modes: ingestion/publish time ranges, feed include/exclude, title keyword, URL domain, minimum body length, previously matched intent, and language.
- One page can span both stores; the cursor is a single object either way. Do not try to page the two stores separately.
- `matched_intent_ids` works across the whole timeline. For recent articles it resolves against the live `match_seen` table; for archived ones against the snapshot taken at archive time. Event-mode intents never write `match_seen` and therefore never appear.
- The live half of that table is reset when an intent's `text` / sub-texts change, when a summary-history row is deleted, and when the intent is deleted. If it resolves to nothing, the recent window is not queried at all and a `warnings` entry says so — treat that as "the match log was reset", not "the intent matched nothing recently".
- For "what would this stored intent match right now", use `/api/external/intents/{id}/fire` instead — that runs the intent's own vector and threshold.

Search returns `mode`, `hits`, `warnings`, and `next_cursor`. Always surface non-empty `warnings`: they distinguish degraded feed-name lookup, a degraded matched-intent lookup, an embedding model mismatch, a pagination boundary too large to exclude safely, and a derived-field backfill still in progress (in which case filters on publication time, language, url domain or body length may miss articles ingested before that deployment. Those articles are in the **recent** window, not the deep archive — archived articles were enriched individually and are complete; filtering on `ingested_at_ts` instead is unaffected).

A failure in either store fails the whole request rather than returning half the timeline with a 200, so a successful response is always a complete answer for the filters you sent.

`GET /api/dashboard/maintenance/qdrant_stats` returns:

```jsonc
{
  "segments": {
    "current": {
      "points_count": 113000,
      "earliest_ingested_at_ts": 1780000000,
      "latest_ingested_at_ts": 1785000000,
      "alias_ok": true,
      "derived_backfill_pending": 0
    },
    "archive": {
      "points_count": 12345,
      "earliest_ingested_at_ts": 1770000000,
      "latest_ingested_at_ts": 1779999999,
      "alias_ok": true
    }
  }
}
```

This is the operator surface, so it does expose the storage split — the search contract does not. `alias_ok=false` means the stable alias does not target the collection for the live embedder generation; treat semantic ranking as unreliable. `null` means the alias check itself failed. `derived_backfill_pending > 0` means some points **in the recent window** still lack the derived filter fields, and those filters under-return until it reaches zero; the archive is complete regardless. The field can also be reported by the search side as *unknown* (a failed count), which triggers the same warning **on purpose** — unknown is treated as pending, so a persistent warning during a Qdrant wobble is expected behaviour, not a bug to retry around.

`derived_backfill_quarantined` counts points the backfill has given up on after repeated failed writes, **since this process started** (a restart resets it to 0 and the next few rounds rediscover them). `null` means the count is unavailable, not that it is zero. It is reported alongside `derived_backfill_pending`, never deducted from it — a quarantined point genuinely lacks its fields.

Physical Qdrant collection names are intentionally absent from the contract.

## Mutate intents

| Method & path | Purpose |
| --- | --- |
| `POST /intents` | Create. Body: `IntentCreate` (see `schemas.md`). |
| `PUT /intents/{id}` | Replace fields. Body: `IntentUpdate`. **Changing `text` clears `match_seen` for this intent** — the next scheduled scan can re-fire articles the operator already saw. |
| `DELETE /intents/{id}` | Remove (cascades `match_seen`; irreversible from the API). |

## Mutate feeds

| Method & path | Purpose |
| --- | --- |
| `POST /feeds` | Add a feed. Body: `FeedCreate`. |
| `PATCH /feeds/{id}` | Rename, retune `poll_interval_minutes`, swap source `config`. |
| `PATCH /feeds/{id}/tags` | Edit just the tag set. |
| `DELETE /feeds/{id}` | Remove (already-ingested articles stay). |

## Fire — test/run on demand

The "test what this intent would match right now" surface. Pick by side-effect profile:

| Method & path | Sync? | Notifier? | Writes `match_seen`? | Mode constraint | Rate limit |
| --- | --- | --- | --- | --- | --- |
| `POST /intents/{id}/fire?lookback=86400&skip_seen=true&threshold=0.60` | No (`202 {task_id, status_url}`; poll `GET /intents/{id}/fire/{task_id}`) | **Yes** | No (both fire paths skip `match_seen` writes) | cron-mode only (event → 409) | 1 / intent / 60 s |
| `POST /api/external/intents/{id}/fire` (body: `ExternalFireRequest`) | **Yes** — matches + LLM summary in the response | **No** (designed for agents) | No | cron-mode only (event → 409) | 1 / intent / 60 s |
| `POST /feeds/{id}/fire?dry_run=true` | No (`202 {task_id}`; poll `GET /feeds/{id}/fire/{task_id}`) | n/a | `dry_run=true` → no DB writes | n/a | 1 / feed / 60 s |

`ExternalFireRequest` has `extra="forbid"` — unknown fields → **422**. `threshold` accepts `0.20–0.95` here (wider than the `0.60–0.95` at intent-create time) so you can sweep low during diagnostics without first PUTting the intent.

`ExternalFireResponse` shape:

```jsonc
{
  "intent_id": 42,
  "match_count": 7,
  "matches": [
    {
      "article_id": "a1b2c3...",                  // MD5(url+title) = Qdrant point id
      "score": 0.84,                              // cosine similarity
      "title": "…",
      "url": "https://…",
      "published_at": "2026-05-12T14:01:00+00:00",  // may be null
      "feed_id": 9                                  // may be null
    }
  ],
  "summary": "## Headline takeaways\n- …",        // markdown — same body that would have emailed
  "summary_error": null                            // populated if LLM call failed; then `summary` is null
}
```

Async fire status payload (`GET /intents/{id}/fire/{task_id}`):

```jsonc
{
  "task_id": "...",
  "intent_id": 42,
  "status": "pending" | "running" | "succeeded" | "failed" | "cancelled",
  "started_at": "…", "finished_at": "…",
  "match_count": 7,
  "matches": [/* … same shape as ExternalFireResponse.matches */],
  "pushed": true,                                  // notifier delivery outcome
  "push_error": null
}
```

## Translate (agent utility)

| Method & path | Purpose |
| --- | --- |
| `POST /intents/translate` | Stateless one-shot translation via the summarizer LLM. Body: `{"source_text": "...", "target_language": "en"}` (`source_text` ≤ 2000 chars) → `{"text": "..."}`. Useful before creating an intent — translate the intent text into the operator's preferred language without persisting anything. `502` if the LLM call fails; `503` if the backend isn't ready yet. |

`target_language` accepts values matching `[A-Za-z][A-Za-z0-9_\- ]*` (e.g. `"en"`, `"zh"`, `"Japanese"`).

## Templates and settings (read freely; mutate with care)

| Method & path | Purpose |
| --- | --- |
| `GET /api/settings/schema` | What env vars are tunable, their types and ranges. **Read this before suggesting any `.env` change** — the schema is authoritative. |
| `GET /api/settings/values` | Current values (sensitive ones masked). |
| `POST /api/settings/save` | Write back to `.env`. **Can trigger a process restart** (lifespan SIGTERMs itself when secret env vars change). Require explicit operator consent. |
| `GET /api/prompts/templates` | List template names by kind (`system` / `instruction`). Use the returned names in `IntentCreate.system_template` / `instruction_template`. |
| `GET /api/prompts/templates/{kind}/{name}` | Full template detail (name, kind, body). |
| `POST /api/prompts/templates/{kind}` | Create a new template by **cloning** an existing one (raw-content creation isn't supported here). Body: `{"name": "<new-unique-name>", "source": "default"}`; `source` defaults to `"default"`, pass another template name to clone from it. Returns `201`. To set custom content, follow up with PUT. |
| `PUT /api/prompts/templates/{kind}/{name}` | Overwrite template content (rejects builtin names with `403`). Body: `{"content": "<full Jinja2 template text>"}`. |
| `DELETE /api/prompts/templates/{kind}/{name}` | Remove a template (204). |
| `POST /api/prompts/templates/{kind}/{name}/rename` | Rename a template. Body: `{"new_name": "..."}`. |

Template edits take effect on the next scheduler tick.

## History (persisted cron summaries)

| Method & path | Purpose |
| --- | --- |
| `GET /intents/{intent_id}/history?since=YYYY-MM-DD&until=YYYY-MM-DD&limit=50&offset=0` | List persisted summary rows for a cron-mode intent. Dates are interpreted in the intent's timezone. |
| `DELETE /intents/{intent_id}/history/{row_id}` | Delete one history row and evict its citations from `match_seen` so a re-backfill can re-fire them. Returns 204. |
| `POST /intents/{intent_id}/backfill` | Replay past cron fire-times through the scan+summarize pipeline. Body: `{"since": "YYYY-MM-DD", "until": "YYYY-MM-DD"}` (optional; defaults to Qdrant-bounded range). Returns `202 {task_id, status_url}`. |
| `GET /intents/{intent_id}/backfill/{task_id}` | Poll backfill status. Shape: `{"task_id", "status": "pending"|"running"|"done"|"error", "progress": {"done": N, "total": M}, "error": null|"..."}`. |
| `POST /intents/{intent_id}/history/aggregate` | Generate an LLM aggregate over selected history rows. Body: `{"since": "...", "until": "...", "subject": "..."}` (subject optional). Returns `{intent_id, summary, rows_used, rows_total}`. |
| `POST /intents/{intent_id}/history/aggregate/send` | Same as aggregate but also dispatches the result via the intent's configured channels. Body: `{"since": "...", "until": "...", "subject": "..."}`. Returns per-channel outcome list with HTTP status reflecting overall success. |
| `GET /intents/{intent_id}/history/export?since=YYYY-MM-DD&until=YYYY-MM-DD` | Export history rows as pretty-printed JSON (`indent=2`). Suitable for backup or external analysis. |

All history endpoints require the intent to exist and have a cron-mode schedule. Event-mode intents return an empty list from `GET /history` and 422 from aggregate/backfill endpoints.
