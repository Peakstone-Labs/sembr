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
