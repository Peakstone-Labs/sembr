# Endpoint surface

Authoritative schema: `GET /openapi.json`. This page is a curated subset tracking sembr **1.0**.

## Sanity / discovery (read-only)

| Method & path | Purpose |
| --- | --- |
| `GET /health` | Is the stack up? `{"status":"ok"}` ⇒ yes. `503` ⇒ embedder probe still warming — retry in 30 s. **No auth.** |
| `GET /intents` | List every intent. |
| `GET /intents/{id}` | Full record for one intent. |
| `GET /feeds` | List every feed (RSS / NewsAPI / Twitter). |

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
| `POST /intents/{id}/fire?lookback=86400&skip_seen=true&threshold=0.75` | No (`202 {task_id, status_url}`; poll `GET /intents/{id}/fire/{task_id}`) | **Yes** | No (both fire paths skip `match_seen` writes) | cron-mode only (event → 409) | 1 / intent / 60 s |
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

## Templates and settings (read freely; mutate with care)

| Method & path | Purpose |
| --- | --- |
| `GET /api/settings/schema` | What env vars are tunable, their types and ranges. **Read this before suggesting any `.env` change** — the schema is authoritative. |
| `GET /api/settings/values` | Current values (sensitive ones masked). |
| `GET /api/prompts/templates` | List system + instruction prompt templates by name. Use the returned names in `IntentCreate.system_template` / `instruction_template`. |
| `POST /api/settings/save` | Write back to `.env`. **Can trigger a process restart** (lifespan SIGTERMs itself when secret env vars change). Require explicit operator consent. |
| `POST/PUT/DELETE /api/prompts/templates/...` | Template CRUD. Edits take effect on the next scheduler tick. |
