# AGENTS.md — Driving sembr from an AI Agent

> **This document teaches an AI agent (Claude Code, Cursor, Cline, Aider, a custom LangChain / DSPy / OpenAI-SDK script — anything that can speak HTTP) how to operate a **running** sembr instance.**
>
> It is **not** the install guide. If sembr isn't running yet, see the sibling [`INSTALL.md`](INSTALL.md). Companion to [`public_install.md`](public_install.md) (public-exposure / reverse-proxy walkthrough rewritten for agent execution).
>
> Designed to fit in one context window: ~500 lines, no decorative prose, copy-pasteable curl + Python `httpx` for every flow.

## 1. Mental model — what is sembr, in one paragraph

sembr is a **self-hosted intent radar**. The user (or you, the agent) defines a natural-language *intent* — e.g. "Fed policy moves that affect EM currencies" or "OpenAI / Anthropic product releases". sembr stores the intent as a vector, continuously ingests articles from RSS / NewsAPI.ai / Twitter, vector-matches new articles against every active intent on a schedule, and pushes an LLM-analyzed digest via email (Telegram / Discord / Slack scaffolded for post-1.0). This is **"Reverse RAG"** — vectors-as-queries, articles-as-data, rather than the usual articles-as-queries-into-document-store.

You'll mostly be calling four things: list/create **intents**, list/create **feeds**, **fire** an intent on demand (for testing or one-off scans), and read back the **fire-task result**.

## 2. Base URL and auth

```
BASE = http://<host>:<port>           # default http://localhost:8000
```

- **Where `<host>` and `<port>` come from:** ask the operator, or read `SEMBR_BIND_ADDR` + `SEMBR_HOST_PORT` from `.env`. Typical: `localhost:8000` on the same box; `<LAN-IP>:8000` from another LAN device; `https://<your-domain>` if behind a reverse proxy.
- **Auth:** if the operator set `DASHBOARD_TOKEN` in `.env` (recommended whenever the host is reachable beyond loopback), send it on every `/api/*` request:

  ```
  X-Dashboard-Token: <DASHBOARD_TOKEN value>
  ```

  Wrong/missing token → `401 Unauthorized`. If `DASHBOARD_TOKEN` is empty, auth is bypassed (intentional for local-dev). The non-`/api/*` paths — `/health`, `/intents`, `/feeds` — are currently unauthenticated by design; this will tighten in a future release, so write code that **sends the header on every request, including the unauthenticated paths**. The header is ignored if the route doesn't require it.

- **Content-Type:** every POST/PUT/PATCH body is JSON. Set `Content-Type: application/json`.

## 3. Endpoint surface — pick the one you need

### Sanity / discovery

| Method & path | Purpose |
| --- | --- |
| `GET  /health` | Is the stack up? `{"status":"ok"}` ⇒ yes. 503 ⇒ embedder probe failing — call again in 30s. |
| `GET  /intents` | List every intent the operator has defined. |
| `GET  /intents/{id}` | Full record for one intent. |
| `GET  /feeds` | List every feed (RSS / NewsAPI / Twitter source). |

### Mutate intents

| Method & path | Purpose |
| --- | --- |
| `POST /intents` | Create a new intent. Body: `IntentCreate`. |
| `PUT  /intents/{id}` | Replace fields on an intent. Body: `IntentUpdate`. **Changing `text` clears prior `match_seen`** for this intent so a rescan over the same lookback can re-fire matches against the new wording. |
| `DELETE /intents/{id}` | Remove an intent (cascades `match_seen`). |

### Mutate feeds

| Method & path | Purpose |
| --- | --- |
| `POST /feeds` | Add a feed. Body: `FeedCreate`. |
| `PATCH /feeds/{id}` | Rename, retune polling interval, swap source config. |
| `PATCH /feeds/{id}/tags` | Just edit the tag set. |
| `DELETE /feeds/{id}` | Remove a feed (won't delete already-ingested articles). |

### Fire — test/run on demand

These are the endpoints you'll use most for **agent-driven flows**: "I just created an intent, did it actually match anything in the last 24h?"

| Method & path | Synchronous? | Notifier (email) fires? | Writes `match_seen`? | Mode constraint | Rate limit |
| --- | --- | --- | --- | --- | --- |
| `POST /intents/{id}/fire?lookback=86400&skip_seen=true&threshold=0.75` | No — returns `202 {task_id, status_url}`, poll `GET /intents/{id}/fire/{task_id}` | **Yes** | No (both fire paths are read-only against `match_seen`) | cron-mode only (event intents → 409) | 1 per intent per 60 s |
| `POST /api/external/intents/{id}/fire` (body: `{lookback_seconds, threshold, skip_seen, feed_ids}` — all optional, fall back to the intent's stored values) | **Yes** — matches + LLM summary in the response | **No** (designed for programmatic / agent use) | No | **cron-mode intents only** (event-mode → 409) | 1 per intent per 60 s |
| `POST /feeds/{id}/fire?dry_run=true` | No — `202 {task_id}`, poll `GET /feeds/{id}/fire/{task_id}` | n/a | Dry-run = no DB writes | n/a | 1 per feed per 60 s |

**Rule of thumb for agents:** use `POST /api/external/intents/{id}/fire` — it's synchronous, doesn't email anyone, and the response carries both the match list and the LLM-generated summary. Reserve `POST /intents/{id}/fire` for "I actually want the notifier to fire" cases (e.g. on-demand digest send).

Note: `ExternalFireRequest` has `extra="forbid"` — sending unknown fields → 422. `threshold` accepts a wider range here (0.20–0.95) than at intent-create time (0.60–0.95) so you can sweep low values during exploration without first PUTting the intent.

### Templates / settings — read-only first

| Method & path | Purpose |
| --- | --- |
| `GET /api/settings/schema` | Discover what env vars are tunable and their types. |
| `GET /api/settings/values` | Read the current settings (sensitive values masked). |
| `GET /api/prompts/templates` | List system + instruction prompt templates by name. |

Mutating settings (`POST /api/settings/save`) or templates (`POST/PUT/DELETE /api/prompts/templates/...`) **triggers a process restart** in some cases (the lifespan SIGTERMs itself when secret env vars change). **Don't touch these without an explicit user instruction** — surface what you'd change and ask first.

## 4. Schemas you'll actually need

### `IntentCreate` body

The discriminated unions (`channels`, `schedule`) are where most agents trip up. Use these exact shapes.

```jsonc
{
  "name": "openai-anthropic-releases",      // 1–100 chars, unique-ish (server enforces)
  "text": "OpenAI, Anthropic, and DeepMind product launches and benchmark releases. Exclude blog-only opinion pieces.",
  "sub_texts": [],                          // optional; up to 3 multilingual phrasings (each {language, text})
  "threshold": 0.75,                        // 0.60–0.95; lower = more permissive
  "enabled": true,
  "channels": [                             // 1–10 entries. Currently only "email".
    {
      "type": "email",
      "to": ["you@example.com"],            // 1–10 addresses
      "cc": [],                             // optional
      "bcc": []                             // optional
    }
  ],
  "schedule": {                             // pick ONE of the two shapes below
    "mode": "cron",                         // OR "event"
    "preset": "daily",                      // "daily" | "weekly" | "hourly"
    "hour": 9,
    "minute": 0,
    "weekday": "mon",                       // required only when preset="weekly"
    "lookback_seconds": 86400,              // 300–2_592_000 (5 min to 30 d)
    "skip_seen": true                       // dedupe against prior runs
  },
  "feed_filter": null,                      // null = scan ALL feeds; {"ids":[1,3]} = subset; {"ids":[]} = nothing (intent is paused)
  "timezone": "Asia/Shanghai",              // IANA tz; affects digest rendering and cron firing time
  "language": "zh",                         // digest output language; "en", "zh", etc.
  "system_template": "default",             // template names from GET /api/prompts/templates
  "instruction_template": "default"
}
```

Event-schedule alternative:

```jsonc
{
  "mode": "event",
  "trigger_count": 3,                       // fire after this many articles cross threshold
  "max_wait_seconds": 3600                  // even if trigger_count not reached, fire after this long
}
```

### `FeedCreate` body

```jsonc
{
  "name": "Reuters Top News",
  "url": "https://www.reuters.com/world/rss",   // for RSS — http(s):// URL
  "source_type": "rss",                          // "rss" | "newsapi" | "twitter"
  "config": {},                                  // source-type-specific knobs; {} = use defaults
  "poll_interval_minutes": 30,                   // 5–1440
  "tags": ["news", "finance"]                    // kebab-case, 0–10 tags
}
```

For NewsAPI:

```jsonc
{
  "name": "NewsAPI: BBC",
  "url": "bbc.com",                              // the source's host as NewsAPI.ai labels it
  "source_type": "newsapi",
  "config": {"sourceUri": "bbc.com"},
  "poll_interval_minutes": 30,
  "tags": ["news", "newsapi"]
}
```

For Twitter (requires `TWITTER_AUTH_TOKEN` in `.env`):

```jsonc
{
  "name": "Elon Musk",
  "url": "elonmusk",                             // the screen-name only, no @, no URL
  "source_type": "twitter",
  "config": {"screen_name": "elonmusk"},
  "poll_interval_minutes": 30,
  "tags": ["twitter"]
}
```

## 5. Curl recipes — copy/paste

Set these once:

```bash
BASE=http://localhost:8000
TOKEN=...                                   # from .env DASHBOARD_TOKEN, or empty
H_TOKEN=(-H "X-Dashboard-Token: ${TOKEN}")  # bash array; expand as "${H_TOKEN[@]}"
H_JSON=(-H "Content-Type: application/json")
```

### Health probe

```bash
curl -sf "${BASE}/health" "${H_TOKEN[@]}"
```

### List intents

```bash
curl -s "${BASE}/intents" "${H_TOKEN[@]}" | jq '.[] | {id, name, enabled, threshold}'
```

### Create an intent

```bash
curl -s -X POST "${BASE}/intents" "${H_JSON[@]}" "${H_TOKEN[@]}" -d '{
  "name": "openai-anthropic-releases",
  "text": "OpenAI, Anthropic, and DeepMind product launches and benchmark releases.",
  "threshold": 0.75,
  "channels": [{"type":"email","to":["you@example.com"]}],
  "schedule": {"mode":"cron","preset":"daily","hour":9,"minute":0,"lookback_seconds":86400,"skip_seen":true},
  "timezone": "Asia/Shanghai",
  "language": "zh"
}' | jq '{id, name}'
```

### Update an intent's wording (clears `match_seen` for it)

```bash
INTENT_ID=42
curl -s -X PUT "${BASE}/intents/${INTENT_ID}" "${H_JSON[@]}" "${H_TOKEN[@]}" -d '{
  "text": "OpenAI / Anthropic / DeepMind / xAI product launches, model releases, and benchmark wins.",
  "threshold": 0.72
}'
```

### Sync fire — best for agents (no email, no `match_seen` poison)

```bash
INTENT_ID=42
curl -s -X POST "${BASE}/api/external/intents/${INTENT_ID}/fire" "${H_JSON[@]}" "${H_TOKEN[@]}" -d '{
  "lookback_seconds": 86400,
  "threshold": 0.70,
  "skip_seen": false,
  "feed_ids": null
}' | jq '{match_count, matches: .matches | map({title, score, url, published_at, feed_id}), summary: (.summary // "<no summary>" | .[0:200])}'
```

Response shape (`ExternalFireResponse`):

```jsonc
{
  "intent_id": 42,
  "match_count": 7,
  "matches": [
    {
      "article_id": "a1b2c3...",                              // MD5(url+title) — the in-Qdrant point id
      "score": 0.84,
      "title": "Anthropic ships Claude Sonnet 4.6",
      "url": "https://...",
      "published_at": "2026-05-12T14:01:00+00:00",            // string, may be null
      "feed_id": 9                                            // may be null for un-attributed sources
    }
    /* ... */
  ],
  "summary": "## Headline takeaways\n- ...",                  // markdown — the same body that would be emailed
  "summary_error": null                                        // populated if the LLM call failed; summary is then null
}
```

### Async fire — when you DO want the notifier to fire

```bash
TASK=$(curl -s -X POST "${BASE}/intents/${INTENT_ID}/fire?lookback=86400&skip_seen=true" "${H_TOKEN[@]}")
TASK_ID=$(echo "${TASK}" | jq -r .task_id)

# Poll until terminal
while :; do
  STATE=$(curl -s "${BASE}/intents/${INTENT_ID}/fire/${TASK_ID}" "${H_TOKEN[@]}")
  STATUS=$(echo "${STATE}" | jq -r .status)
  echo "task ${TASK_ID}: ${STATUS}"
  case "${STATUS}" in
    succeeded|failed|cancelled) break ;;
  esac
  sleep 3
done
echo "${STATE}" | jq .
```

`status` cycles: `pending → running → succeeded | failed | cancelled`. The terminal payload contains `matches`, `summary_preview`, and any `error`.

### List feeds

```bash
curl -s "${BASE}/feeds" "${H_TOKEN[@]}" | jq '.[] | {id, name, source_type, poll_interval_minutes, tags}'
```

### Add an RSS feed and immediately dry-run it

```bash
FEED=$(curl -s -X POST "${BASE}/feeds" "${H_JSON[@]}" "${H_TOKEN[@]}" -d '{
  "name": "Hacker News Front Page",
  "url": "https://hnrss.org/frontpage",
  "source_type": "rss",
  "poll_interval_minutes": 30,
  "tags": ["tech","hn"]
}')
FEED_ID=$(echo "${FEED}" | jq -r .id)

# Dry-run: fetch + parse + diff without writing to DB
DRY_TASK=$(curl -s -X POST "${BASE}/feeds/${FEED_ID}/fire?dry_run=true" "${H_TOKEN[@]}")
DRY_TASK_ID=$(echo "${DRY_TASK}" | jq -r .task_id)
# poll GET /feeds/${FEED_ID}/fire/${DRY_TASK_ID} same as above
```

## 6. Python `httpx` example — full workflow

```python
import httpx, time, json

BASE = "http://localhost:8000"
TOKEN = "..."   # DASHBOARD_TOKEN from .env, or "" if unset

HEADERS = {"X-Dashboard-Token": TOKEN, "Content-Type": "application/json"}

def _json(r: httpx.Response) -> dict:
    r.raise_for_status()
    return r.json()

with httpx.Client(base_url=BASE, headers=HEADERS, timeout=30.0) as c:

    # 1. Sanity
    _json(c.get("/health"))

    # 2. Create an intent
    intent = _json(c.post("/intents", json={
        "name": "fed-em-currencies",
        "text": "US Federal Reserve policy moves that impact emerging-market currencies.",
        "threshold": 0.72,
        "channels": [{"type": "email", "to": ["analyst@example.com"]}],
        "schedule": {
            "mode": "cron", "preset": "daily", "hour": 7, "minute": 30,
            "lookback_seconds": 86400, "skip_seen": True,
        },
        "timezone": "America/New_York",
        "language": "en",
    }))
    intent_id = intent["id"]

    # 3. Sync-fire to see what would match RIGHT NOW (no email side-effect)
    result = _json(c.post(f"/api/external/intents/{intent_id}/fire", json={
        "lookback_seconds": 86400,
        "threshold": 0.70,           # slightly lower for the diagnostic run
        "skip_seen": False,
        "feed_ids": None,
    }))
    print(f"matched {result['match_count']} articles")
    for m in result["matches"][:5]:
        print(f"  {m['score']:.3f}  {m['title']}  (feed_id={m['feed_id']})")
    if result.get("summary"):
        print("\nSummary preview:\n" + result["summary"][:400])

    # 4. If 0 matches, try lowering threshold; if 200+, raise it
    if result["match_count"] < 3:
        _json(c.put(f"/intents/{intent_id}", json={"threshold": 0.68}))
    elif result["match_count"] > 50:
        _json(c.put(f"/intents/{intent_id}", json={"threshold": 0.78}))

    # 5. Hand off to the scheduled cron — daily 07:30 NY time. No more action needed.
```

## 7. Error contract — what each HTTP status means

| Status | Cause | Agent action |
| --- | --- | --- |
| `200` / `201` | Success | Continue. |
| `202` | Async task accepted | Poll the matching `GET .../fire/{task_id}` endpoint. |
| `204` | Success, no body | Continue (used by DELETE and a few PATCH paths). |
| `400` | Malformed request not caught by schema (rare) | Read the JSON `detail`; fix the request. |
| `401` | Missing / wrong `X-Dashboard-Token` | Add header or ask the operator for the token. |
| `404` | Intent / feed / task ID doesn't exist | Re-list, don't retry the same ID. |
| `409` | Mode constraint — e.g. calling `/api/external/.../fire` on an event-mode intent | Don't retry; either switch endpoints or change the intent's schedule. |
| `422` | Pydantic validation failure (incl. `extra="forbid"` on `ExternalFireRequest`) | Read `detail[].loc` + `detail[].msg`; the offending field is named explicitly. |
| `429` | Rate-limited (fire endpoints: 1 per intent/feed per 60 s) | Sleep ≥60 s, then retry. Check the `Retry-After` header if present. |
| `500` / `503` | sembr-side error / embedder still warming | If 503 on `/health`, sleep 30 s and retry; if 500 elsewhere, surface to operator. |

Error response body is always shaped:

```jsonc
{"detail": "..."}                   // for plain HTTP errors
// OR
{"detail": [{"loc": ["body", "schedule", "preset"], "msg": "...", "type": "..."}]}   // for 422
```

## 8. Things agents should and should NOT do

**Do:**

- Use `POST /api/external/intents/{id}/fire` for any "test this intent" flow. It's idempotent and side-effect-free.
- When unsure whether an intent is currently catching what the user wants, sync-fire with a slightly **lower** threshold and report the score distribution rather than just `match_count`. Lets the operator pick a threshold informed by real scores.
- Always include `X-Dashboard-Token` even on currently-unauthenticated paths (forwards-compat).
- Read `/api/settings/schema` before suggesting any `.env` change — the schema is authoritative for which knobs exist and their valid ranges.
- Honour the rate limit (1 fire per intent/feed per 60 s). 409 means back off, not retry harder.

**Don't:**

- Don't `POST /api/settings/save` without explicit operator approval — it can restart the process.
- Don't `POST /intents/{id}/fire` (the non-`external` one) for diagnostics — that one fires the notifier and emails the operator's recipients. Use the external endpoint for tests.
- Don't `DELETE` intents or feeds without confirming with the operator; deletion of an intent cascades `match_seen` and is not reversible from the API.
- Don't `PUT /intents/{id}` with a new `text` casually — that clears the intent's stored `match_seen`, so the very next scheduled cron run can re-fire articles the operator already saw. Surface the side-effect to the operator before mutating `text`.
- Don't invent endpoints. If a flow isn't in this document, list `GET /openapi.json` and read the schema — don't guess.
- Don't store the `DASHBOARD_TOKEN` in any committed file. It's per-deployment.

## 9. Discovery & versioning

- `GET /openapi.json` — full OpenAPI 3.1 schema. Authoritative; this document is a curated subset.
- `GET /docs` — Swagger UI (if you're working interactively with a human).
- `GET /redoc` — ReDoc alternative.
- This file tracks sembr **1.0**. Breaking API changes will get a new major version and a migration note; check the repo's `CHANGELOG.md` if anything in here disagrees with what the server returns.

## 10. Companion docs

- `INSTALL.md` (sibling) — getting sembr running (agent-driven install guide).
- `public_install.md` (sibling) — agent-driven public-exposure walkthrough (reverse proxy + TLS + firewall). For the operator-facing version, see `../docs/deployment/public.md` in the same repo.
- `../docs/architecture.md`, `../docs/configuration.md`, `../docs/modules/*.md` — operator-facing internals.
- `../CLAUDE.md` — internal dev-time guidance (not for agents driving the API; for agents editing the codebase).
