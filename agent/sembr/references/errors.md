# Error contract

| Status | Cause | Agent action |
| --- | --- | --- |
| `200` / `201` | Success | Continue. |
| `202` | Async task accepted | Poll the matching `GET .../fire/{task_id}` endpoint. |
| `204` | Success, no body | Continue (used by DELETE and a few PATCH paths). |
| `400` | Malformed request not caught by schema (rare) | Read `detail`; fix the request. |
| `401` | Missing / wrong `X-Dashboard-Token` | Add the header, or ask the operator for the token. |
| `404` | Intent / feed / task ID doesn't exist | Re-list — don't retry the same ID. |
| `409` | Mode constraint — e.g. firing `/api/external/.../fire` on an **event-mode** intent | Either change the intent's `schedule.mode`, or use a different endpoint. Don't retry. |
| `422` | Pydantic validation (including `extra="forbid"` on `ExternalFireRequest`) | Read `detail[].loc` and `detail[].msg` — the offending field is named explicitly. |
| `429` | Rate-limited (fire endpoints: 1 / intent or feed / 60 s) | Sleep ≥60 s; check `Retry-After` if present. |
| `500` | sembr-side error | Surface to the operator with the raw `detail` (already scrubbed of paths / URLs / tracebacks on the external surface). |
| `503` | `/health` only: embedder probe still warming | Sleep 30 s and retry. |

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
