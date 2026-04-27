# sembr

> Semantic news monitoring via reverse RAG.

**sembr** lets you write a natural-language intent once — _"monitor Fed policy impact on emerging-market currencies"_ — and receive matching news pushed to Telegram, Discord, or Slack as it arrives. No keyword lists, no hand-tuned filters.

## How it differs from traditional RAG

Traditional RAG: user query → search documents → answer.
**Reverse RAG (sembr)**: pre-stored intent vector → continuously scan incoming articles → push semantic matches.

You describe what you care about once. sembr does the watching.

## Quickstart

Requires Docker + Docker Compose. First image pull (`qdrant/qdrant:v1.17.1` ~100MB compressed, `python:3.12` ~140MB compressed) can take 5–10 minutes on a slow connection — `/health` returns 503 until both services are up, which is expected.

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
cp .env.example .env            # required — Compose loads .env for settings
docker compose up --build

# in another shell, once both containers are running:
curl -i http://localhost:8000/health
# expected: HTTP/1.1 200 OK
# {"status":"ok","components":{"qdrant":"ok","sqlite":"ok","embedder":"not_loaded"}}
```

The `embedder` field is intentionally `"not_loaded"` in 0.1.0 — model loading lands in a later release and does not gate `/health`.

**Filesystem note**: keep `./data/` on a local POSIX-ish filesystem (ext4 / APFS / NTFS local). SQLite WAL is unsafe on network shares (NFS, SMB, virtio-9p).

**Port override**: set `SEMBR_HOST_PORT=8080` in `.env` (or as a shell env var) to expose the API on `localhost:8080`. `API_PORT` controls the in-container bind port and should stay at `8000`. See `.env.example` for the full settings surface.

## RSS Feeds

sembr comes with 23 pre-loaded free RSS sources. They start collecting on first launch — no configuration needed.

### Pre-loaded sources

| Category | Sources |
| -------- | ------- |
| International news | AP News, BBC, CNN, The Guardian, Al Jazeera, NPR, Washington Post |
| International finance | Bloomberg Markets, Financial Times, The Economist, WSJ, Nikkei Asia, MarketWatch, Seeking Alpha, Investing.com |
| Asia-Pacific | NYT World, SCMP |
| Chinese finance (via RSSHub) | 华尔街见闻, 财联社电报, 第一财经, 36氪, 虎嗅 |
| Chinese general (via RSSHub) | 澎湃新闻 |

Chinese sources route through the bundled [RSSHub](https://rsshub.app/) sidecar (`rsshub:1200`), which starts automatically alongside the API.

### Add a feed

```bash
curl -X POST http://localhost:8000/feeds \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Hacker News",
    "url": "https://hnrss.org/frontpage",
    "poll_interval_minutes": 30
  }'
# returns the created Feed object with its id
```

`poll_interval_minutes` must be between 5 and 1440. The feed starts collecting immediately — no restart needed.

### List feeds

```bash
curl http://localhost:8000/feeds
```

### Delete a feed

```bash
# get the feed id from GET /feeds first
curl -X DELETE http://localhost:8000/feeds/3
# 204 No Content on success
```

Deleted feeds do not come back on restart. To restore a pre-loaded source, re-add it via POST.

### Data persistence

Feed list and collected article fingerprints are stored in `./data/sembr.db` (SQLite, bind-mounted from the host). They survive `docker compose up --build`, container restarts, and image rebuilds. Only `rm -rf ./data/` permanently deletes them.

## Status

🚧 Pre-release — under active development. The 0.1.0 MVP targets:

- RSS ingestion + BGE-M3 embeddings + Qdrant vector store
- Intent CRUD via REST API
- 5-minute scheduled semantic matching with configurable threshold
- LLM-generated summaries (OpenAI / DeepSeek / local)
- Telegram push delivery

## License

Apache-2.0

## Built by

[Peakstone Labs](https://github.com/Peakstone-Labs) — AI-native quantitative research.
