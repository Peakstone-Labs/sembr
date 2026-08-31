# Error contract

| Status | Cause | Agent action |
| --- | --- | --- |
| `200` / `201` | Success | Continue. |
| `202` | Async task accepted | Poll the matching `GET .../fire/{task_id}` endpoint. |
| `204` | Success, no body | Continue (used by DELETE and a few PATCH paths). |
| `400` | Malformed request not caught by schema; news search also uses this when Qdrant explicitly rejects request content (e.g. malformed point ids) or when `matched_intent_ids` selects more than 20 000 articles | Read `detail`; fix the request. Don't retry unchanged, and don't narrow the time window to escape the intent-id ceiling — it is independent of it. |
| `401` | Missing / wrong `X-Dashboard-Token` | Add the header, or ask the operator for the token. |
| `404` | Intent / feed / task ID doesn't exist | Re-list — don't retry the same ID. A missing collection/alias is service-side and maps to 503, not 404. |
| `409` | Mode constraint — e.g. firing `/api/external/.../fire` on an **event-mode** intent | Either change the intent's `schedule.mode`, or use a different endpoint. Don't retry. |
| `422` | Pydantic validation (including `extra="forbid"` on `ExternalFireRequest` and `NewsSearchRequest`) | Read `detail[].loc` and `detail[].msg` — the offending field is named explicitly. For news search, also check mode consistency and empty include filters. |
| `429` | Rate-limited (fire endpoints: 1 / intent or feed / 60 s) | Sleep ≥60 s; check `Retry-After` if present. |
| `500` | sembr-side error | The `detail` string on external endpoints is already scrubbed (no paths/URLs/tracebacks). Surface it to the operator; for full diagnostics check `docker compose logs api`. |
| `502` | An operator endpoint's Qdrant query failed (e.g. `GET /api/dashboard/maintenance/qdrant_stats`) | Same class as 503: back off, retry, and report the outage. Not a request-shape problem. |
| `503` | Service not ready/unavailable. `/health`: a component is warming or unhealthy. News search: embedder unavailable, Qdrant unreachable/rate-limited, a collection/alias missing, or the matched-intent pre-query failed. Either store failing fails the whole request — you never get half the timeline with a 200. | Retry later with bounded backoff and surface the outage. Do not rewrite filters unless the server returned 400/422. |

## Response body shapes

Plain HTTP errors:

```jsonc
{"detail": "Token required"}
```

Validation errors (`422`) — `detail` is an array:

```jsonc
{
  "detail": [
    {
      "loc": ["body", "schedule", "preset"],
      "msg": "Input should be 'daily', 'weekly' or 'hourly'",
      "type": "literal_error"
    }
  ]
}
```

For the external-facing endpoints, error strings are **scrubbed before egress** — paths, URLs, and tracebacks are stripped. If you need full diagnostics, check the operator's container logs (`docker compose logs api`), not the HTTP response.

## Degradation signals returned with HTTP 200

Search responses can succeed while reporting a degraded condition:

- Non-empty search `warnings`: preserve and surface them with the result. There are six: feed-name enrichment failed; the matched-intent lookup failed (so `matched_intents` is unfilled rather than genuinely empty); `matched_intent_ids` resolved to no article in the recent window, so only archived matches were searched; embedding generations differ; cursor pagination cannot safely exclude an unusually large same-second boundary; derived fields are still being backfilled.
- A backfill warning is a **recall** warning: filters on publication time, language, url domain or body length may miss articles ingested before that deployment. Those articles are in the **recent** window, not the deep archive — archived articles were enriched individually and are complete. Publication-time windows can fall back to `ingested_at_ts`, which is unaffected; the language, url-domain and body-length predicates have **no equivalent** while the queue is non-empty, and the affected hits carry `null` in those very fields, so client-side filtering cannot recover them either. Report the result as incomplete rather than presenting the short list as the answer, and note the direction — the gap is in recent coverage.
- `GET /api/dashboard/maintenance/qdrant_stats` with `alias_ok=false`: semantic similarity is unreliable because the alias targets the wrong model generation.
- `alias_ok=null`: the alias check itself failed. Treat health as unknown, not healthy.

These are not reasons to mutate Qdrant or change the model. Report them to the operator.
