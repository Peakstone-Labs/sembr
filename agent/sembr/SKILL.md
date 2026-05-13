---
name: sembr
description: HTTP-API reference for driving a running sembr instance — a self-hosted "intent radar" / Reverse RAG service that vector-matches RSS / NewsAPI / Twitter articles against natural-language intents and emails LLM-analyzed digests. Use when the user asks to create, list, update, delete, or fire (test-run) sembr intents or feeds; when they need IntentCreate / FeedCreate JSON shapes; when they need curl or Python `httpx` recipes against sembr; when they want diagnostic matching via `/api/external/intents/{id}/fire` without notifier side-effects; or when they hit a 401 / 409 / 422 / 429 from a sembr endpoint and need to interpret it.
---

# sembr — driving a running instance

This skill teaches an AI agent to operate a **running** sembr instance over HTTP. If sembr isn't installed yet, that's a different task — see [`../INSTALL.md`](../INSTALL.md) (sibling inside the repo) or [agent/INSTALL.md on GitHub](https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/agent/INSTALL.md) if you've copied this skill bundle out of the repo.

## 1. Mental model

sembr is a self-hosted **intent radar**. The user defines a natural-language *intent* (e.g. "Fed policy moves affecting EM currencies"). sembr stores the intent as a vector, continuously ingests articles from RSS / NewsAPI / Twitter, vector-matches new articles against every active intent on a schedule, and pushes an LLM-analyzed digest by email. This is **Reverse RAG** — vectors-as-queries, articles-as-data.

You'll mostly be touching four resources: **intents** (CRUD), **feeds** (CRUD), **fire** (test-run an intent on demand), and **fire-task results**.

## 2. Base URL and auth

```
BASE = http://<host>:<port>           # default http://localhost:8000
```

- `<host>` and `<port>` come from `SEMBR_BIND_ADDR` + `SEMBR_HOST_PORT` in the operator's `.env`. Typical: `localhost:8000` on the same box, `<LAN-IP>:8000` from another LAN device, `https://<domain>` if behind a reverse proxy.
- If `DASHBOARD_TOKEN` is set, send it on **every** request:
  ```
  X-Dashboard-Token: <token>
  ```
  Wrong/missing → 401 on gated paths. Empty `DASHBOARD_TOKEN` bypasses auth entirely (local-dev only). When the token is set, every path under `/intents`, `/feeds`, `/api/dashboard`, `/api/prompts`, `/api/settings`, `/api/external` (and the corresponding bare paths) is gated. Only `/health` and `/api/dashboard/config` are unauthenticated by design (so monitors and the login page can bootstrap without a token).
- Every POST/PUT/PATCH body is JSON. Set `Content-Type: application/json`.

## 3. Decision — which "fire" endpoint?

This is the question agents get wrong most often.

| If you want to… | Use | Why |
| --- | --- | --- |
| **Test what an intent would match right now**, see matches + LLM summary in one round-trip, NOT email anyone | `POST /api/external/intents/{id}/fire` | Synchronous; no notifier; no `match_seen` writes; idempotent. The agent endpoint. |
| Actually send the digest to the intent's recipients (e.g. "send me today's brief now") | `POST /intents/{id}/fire` | Async (202 + task_id); notifier fires; poll `GET /intents/{id}/fire/{task_id}`. |
| Trigger a feed refetch (test new RSS URL) | `POST /feeds/{id}/fire?dry_run=true` | Async; `dry_run=true` skips DB writes. |

Both intent-fire paths are **cron-mode only** — event-mode intents return **409**. Rate limit: **1 per intent (or feed) per 60 s** → 429.

## 4. Workflow signposts

When the user asks for…

- **The full endpoint list** (and which writes vs. reads) → read [`references/endpoints.md`](references/endpoints.md).
- **To create an intent or feed** (body shape, threshold range, schedule modes, channel discriminated union, source-type configs) → read [`references/schemas.md`](references/schemas.md), then `POST /intents` or `POST /feeds`.
- **Copy-pasteable curl or Python `httpx` recipes** (create intent, sync-fire, async-fire with polling loop, dry-run a new feed) → read [`references/recipes.md`](references/recipes.md).
- **To interpret an HTTP error** (401 / 409 / 422 / 429 / 503, response body shape) → read [`references/errors.md`](references/errors.md).
- **A discovery / sanity check** → `GET /health` (no auth), `GET /intents`, `GET /feeds`. If `/health` returns 503, the embedder probe is still warming — sleep 30 s and retry.

For anything not covered here, the authoritative schema is `GET /openapi.json`. Don't invent endpoints.

## 5. Guardrails — agents commonly violate these

- **Diagnostics → `POST /api/external/intents/{id}/fire`, never `POST /intents/{id}/fire`.** The latter emails the operator's recipients every time. Catastrophic for "I'm just testing".
- **Don't `PUT /intents/{id}` with a new `text` casually.** Changing `text` clears `match_seen` for that intent → the very next cron run can re-fire articles the operator already saw. Surface the side-effect before mutating `text`.
- **Don't `POST /api/settings/save` without explicit operator consent.** Some saves trigger a process restart (lifespan SIGTERMs itself when secret env vars change).
- **Don't `DELETE` intents or feeds without confirming.** Intent delete cascades `match_seen` and isn't reversible from the API.
- **Honour the rate limit.** 429 means sleep ≥60 s, not retry harder. Check `Retry-After` if present.
- **Don't commit / store `DASHBOARD_TOKEN`.** It's per-deployment.
- **Send `X-Dashboard-Token` on every request.** The only token-free paths today are `/health` and `/api/dashboard/config`; everything else under `/intents`, `/feeds`, `/api/dashboard`, `/api/prompts`, `/api/settings`, `/api/external` 401s without it.

## 6. Discovery and version

- `GET /openapi.json` — full OpenAPI 3.1 schema (authoritative).
- `GET /docs` — Swagger UI (interactive, for humans).
- `GET /redoc` — ReDoc alternative.
- This skill tracks sembr **1.0**. If the server returns a field this skill doesn't describe, trust the server and check `CHANGELOG.md` in the repo.

## 7. Companion documents

Paths are given both as in-repo (relative to this `SKILL.md`) and as GitHub URLs, so the bundle works either way — read in-tree, or copied into a foreign `~/.claude/skills/sembr/`.

| Doc | In-repo path | GitHub URL |
| --- | --- | --- |
| Agent-driven install | [`../INSTALL.md`](../INSTALL.md) | [agent/INSTALL.md](https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/agent/INSTALL.md) |
| Agent-driven public exposure | [`../PUBLIC_INSTALL.md`](../PUBLIC_INSTALL.md) | [agent/PUBLIC_INSTALL.md](https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/agent/PUBLIC_INSTALL.md) |
| Operator-facing public exposure | [`../../docs/deployment/public.md`](../../docs/deployment/public.md) | [docs/deployment/public.md](https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/docs/deployment/public.md) |
| Dev-time guidance for editing sembr's code (not driving its API) | [`../../CLAUDE.md`](../../CLAUDE.md) | [CLAUDE.md](https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/CLAUDE.md) |
