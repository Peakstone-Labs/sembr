# Curl and Python recipes

Copy-paste against any running sembr instance. Replace `BASE` and `TOKEN` with your values.

## Setup (bash)

```bash
BASE=http://localhost:8000
TOKEN=...                                   # from .env DASHBOARD_TOKEN, or empty
H_TOKEN=(-H "X-Dashboard-Token: ${TOKEN}")  # bash array; expand as "${H_TOKEN[@]}"
H_JSON=(-H "Content-Type: application/json")
```

## Health probe

```bash
curl -sf "${BASE}/health" "${H_TOKEN[@]}"
```

`503` ⇒ embedder still warming; sleep 30 s and retry.

## List intents

```bash
curl -s "${BASE}/intents" "${H_TOKEN[@]}" | jq '.[] | {id, name, enabled, threshold}'
```

## Create an intent

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

## Update an intent's wording

Changing `text` clears `match_seen` for this intent — surface to the operator.

```bash
INTENT_ID=42
curl -s -X PUT "${BASE}/intents/${INTENT_ID}" "${H_JSON[@]}" "${H_TOKEN[@]}" -d '{
  "text": "OpenAI / Anthropic / DeepMind / xAI product launches, model releases, and benchmark wins.",
  "threshold": 0.72
}'
```

## Sync fire — the agent's default

No email, no `match_seen` writes, full response in one round-trip. Use this for any "test what this intent matches" flow.

```bash
INTENT_ID=42
curl -s -X POST "${BASE}/api/external/intents/${INTENT_ID}/fire" "${H_JSON[@]}" "${H_TOKEN[@]}" -d '{
  "lookback_seconds": 86400,
  "threshold": 0.70,
  "skip_seen": false,
  "feed_ids": null
}' | jq '{match_count, top: .matches[:5] | map({title, score, feed_id}), summary_head: (.summary // "<none>" | .[0:240])}'
```

**Tip — threshold tuning.** When unsure whether an intent catches what the operator wants, sync-fire with a slightly **lower** threshold (e.g. 0.65 instead of the stored 0.75) and report the *score distribution*, not just `match_count`. Lets the operator pick a threshold informed by real scores rather than guessing.

## Async fire — when the notifier SHOULD fire

```bash
TASK=$(curl -s -X POST "${BASE}/intents/${INTENT_ID}/fire?lookback=86400&skip_seen=true" "${H_TOKEN[@]}")
TASK_ID=$(echo "${TASK}" | jq -r .task_id)

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

`status`: `pending → running → succeeded | failed | cancelled`. Terminal payload carries `matches`, `pushed`, `push_error`.

## List feeds

```bash
curl -s "${BASE}/feeds" "${H_TOKEN[@]}" | jq '.[] | {id, name, source_type, poll_interval_minutes, tags}'
```

## Add an RSS feed and dry-run it

```bash
FEED=$(curl -s -X POST "${BASE}/feeds" "${H_JSON[@]}" "${H_TOKEN[@]}" -d '{
  "name": "Hacker News Front Page",
  "url": "https://hnrss.org/frontpage",
  "source_type": "rss",
  "poll_interval_minutes": 30,
  "tags": ["tech","hn"]
}')
FEED_ID=$(echo "${FEED}" | jq -r .id)

DRY=$(curl -s -X POST "${BASE}/feeds/${FEED_ID}/fire?dry_run=true" "${H_TOKEN[@]}")
DRY_TASK_ID=$(echo "${DRY}" | jq -r .task_id)
# Poll GET ${BASE}/feeds/${FEED_ID}/fire/${DRY_TASK_ID} the same way.
```

## Python — full workflow with `httpx`

```python
import httpx

BASE = "http://localhost:8000"
TOKEN = "..."   # DASHBOARD_TOKEN from .env, or "" if unset

HEADERS = {"X-Dashboard-Token": TOKEN, "Content-Type": "application/json"}

def _json(r: httpx.Response) -> dict:
    r.raise_for_status()                          # raise_for_status() returns None — don't chain .json()
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

    # 4. Auto-tune: too few or too many matches → adjust stored threshold
    if result["match_count"] < 3:
        _json(c.put(f"/intents/{intent_id}", json={"threshold": 0.68}))
    elif result["match_count"] > 50:
        _json(c.put(f"/intents/{intent_id}", json={"threshold": 0.78}))

    # 5. Done — daily 07:30 NY-time cron takes over.
```
