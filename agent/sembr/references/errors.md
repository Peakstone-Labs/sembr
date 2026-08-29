# Error contract

| Status | Cause | Agent action |
| --- | --- | --- |
| `200` / `201` | Success | Continue. |
| `202` | Async task accepted | Poll the matching `GET .../fire/{task_id}` endpoint. |
| `204` | Success, no body | Continue (used by DELETE and a few PATCH paths). |
| `400` | Malformed request not caught by schema; archive also uses this when Qdrant explicitly rejects request content (e.g. malformed point ids) | Read `detail`; fix the request. Don't retry unchanged. |
| `401` | Missing / wrong `X-Dashboard-Token` | Add the header, or ask the operator for the token. |
| `404` | Intent / feed / task ID doesn't exist | Re-list — don't retry the same ID. Archive collection/alias absence is service-side and maps to 503, not 404. |
| `409` | Mode constraint — e.g. firing `/api/external/.../fire` on an **event-mode** intent | Either change the intent's `schedule.mode`, or use a different endpoint. Don't retry. |
| `422` | Pydantic validation (including `extra="forbid"` on `ExternalFireRequest` and `ArchiveSearchRequest`) | Read `detail[].loc` and `detail[].msg` — the offending field is named explicitly. For archive, also check mode consistency and empty include filters. |
| `429` | Rate-limited (fire endpoints: 1 / intent or feed / 60 s) | Sleep ≥60 s; check `Retry-After` if present. |
| `500` | sembr-side error | The `detail` string on external endpoints is already scrubbed (no paths/URLs/tracebacks). Surface it to the operator; for full diagnostics check `docker compose logs api`. |
| `503` | Service not ready/unavailable. `/health`: a component is warming or unhealthy. Archive search/stats: embedder unavailable, Qdrant unreachable/rate-limited, or archive collection/alias missing. | Retry later with bounded backoff and surface the outage. Do not rewrite filters unless the server returned 400/422. |

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

## Archive degradation signals returned with HTTP 200

Archive responses can succeed while reporting a degraded condition:

- Non-empty search `warnings`: preserve and surface them with the result. They may say feed-name enrichment failed, embedding generations differ, or cursor pagination cannot safely exclude an unusually large same-second boundary.
- `GET /api/archive/stats` with `alias_ok=false`: semantic similarity is unreliable because the alias targets the wrong model generation.
- `alias_ok=null`: the alias check itself failed. Treat health as unknown, not healthy.

These are not reasons to mutate Qdrant or change the model. Report them to the operator.
