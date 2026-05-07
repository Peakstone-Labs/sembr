# sembr

[中文文档](docs/zh/index.md) · [Documentation](https://peakstone-labs.github.io/sembr)

> Semantic news monitoring via reverse RAG.

**sembr** lets you write a natural-language intent once — _"monitor Fed policy impact on emerging-market currencies"_ — and receive matching news as a digest email. No keyword lists, no hand-tuned filters. Telegram / Discord / Slack channels are scaffolded by the marker-ABC plugin point and are on the post-1.0 roadmap.

## How it differs from traditional RAG

Traditional RAG: user query → search documents → answer.
**Reverse RAG (sembr)**: pre-stored intent vector → continuously scan incoming articles → push semantic matches.

You describe what you care about once. sembr does the watching.

## Quickstart

Requires Docker + Docker Compose. First image pull (`qdrant/qdrant:v1.17.1` ~100MB compressed, `python:3.12` ~140MB compressed) can take 5–10 minutes on a slow connection — `/health` returns 503 until both services are up, which is expected.

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
# 1. Copy the example env file:
cp .env.example .env
# 2. Open .env and replace the placeholder with your real SiliconFlow key:
#    EMBEDDER_API_KEY=sk-your-actual-key-here
# 3. Start all services:
docker compose up --build

# in another shell, once both containers are running:
curl -i http://localhost:8000/health
# expected while embedder probe is running: HTTP/1.1 503 {"status":"degraded","components":{"embedder":"loading",...}}
# expected after probe succeeds:            HTTP/1.1 200 {"status":"ok","components":{"embedder":"ok",...}}
```

`EMBEDDER_API_KEY` is required — the container will exit non-zero immediately at startup if the key is missing or blank. Get a free key at [siliconflow.cn](https://siliconflow.cn).

**Filesystem note**: keep `./data/` on a local POSIX-ish filesystem (ext4 / APFS / NTFS local). SQLite WAL is unsafe on network shares (NFS, SMB, virtio-9p).

**Port override**: set `SEMBR_HOST_PORT=8080` in `.env` (or as a shell env var) to expose the API on `localhost:8080`. `API_PORT` controls the in-container bind port and should stay at `8000`. See `.env.example` for the full settings surface.

## Production deployment

The Settings tab edits the host `.env` and restarts containers in place. To do
that, `docker-compose.yml` mounts two host paths into the API container:

| Mount | Purpose | Risk |
| ----- | ------- | ---- |
| `./.env:/app/.env` | settings editor reads/writes the real `.env` | API can rewrite host config |
| `/var/run/docker.sock:/var/run/docker.sock` | RestartController drives `docker compose up -d --force-recreate --no-deps rsshub` so passthrough env changes take effect | **API container is effectively docker-root on the host** |

Mounting the docker socket is a deliberate single-tenant trade-off (same model
as Watchtower / Portainer). Anyone with API access — or any RCE in the API
process — can launch privileged containers, read other volumes, and escape to
root on the host. Concrete implications:

- **Always set `DASHBOARD_TOKEN`** before exposing the API beyond `localhost`.
  Without it, `/api/settings/*` is reachable by anyone who can reach the port.
- **Do not run sembr on a multi-tenant host** unless you accept that an API
  compromise = host compromise.
- **Prefer a private network** (LAN, VPN, Tailscale) over a public IP. If you
  must expose it publicly, put it behind a reverse proxy with mTLS or basic auth
  _in addition_ to `DASHBOARD_TOKEN`.
- **`cp .env.example .env` BEFORE the first `docker compose up`** — without an
  existing host `.env` file, Docker creates a directory at the bind-mount path
  and the API will refuse to start.

If you cannot accept the docker-socket exposure, run sembr without the Settings
tab: comment out the two volume mounts above; `/api/settings/save` will return
500 on RSSHub restart but the rest of the app keeps working.

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

## Custom prompt templates

sembr ships with two built-in templates (`prompts/system/default.md` and `prompts/instruction/default.md`). To customise the LLM prompt for a specific intent, place your own `.md` files under `prompts/system/` or `prompts/instruction/` on the host and reference them when creating or updating an intent.

```bash
# Write a custom instruction template (100-word crypto summary in Chinese)
cat > prompts/instruction/crypto_zh.md << 'EOF'
用户关注：{intent_text}

以下文章与该主题语义匹配，每条包含标题、正文和来源 URL。

{articles}

---

请用 100 字以内中文总结以上加密货币相关要点。
EOF

# Use the template when creating an intent
curl -X POST http://localhost:8000/intents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "crypto-zh-brief",
    "text": "cryptocurrency Bitcoin Ethereum price movements",
    "instruction_template": "crypto_zh",
    "channels": [{"type":"email","to":["you@example.com"]}]
  }'
```

**Template rules:**

- File name (without `.md`) is the identifier — no path separators, no leading dot, length 1–100 chars, Unicode ok
- `instruction` templates: placeholders `{intent_text}` and `{articles}` (both required)
- `system` templates: placeholder `{language}`
- Edits to files on the host take effect on the **next tick** — no restart needed
- Listing available templates: `GET /api/prompts/templates`
- Preview a template: `GET /api/prompts/templates/instruction/crypto_zh`

## Embedder

sembr ships with **BGE-M3** as its default embedding model — and on [SiliconFlow](https://siliconflow.cn) it's **free to use**. Unlimited semantic monitoring with a top-tier multilingual model at zero inference cost.

**Why BGE-M3?** It's currently the best open-source embedder for Chinese + English mixed news content:

- **1024 dimensions, 8192-token context** — handles full article bodies, not just titles
- **Native bilingual** (CN/EN, plus 100+ languages) — critical for sembr's mixed RSS sources (财联社/Bloomberg/SCMP/Al Jazeera all in one intent)
- **MTEB top-tier on retrieval** — what reverse-RAG actually needs (intent vector ↔ article vector ANN match)
- **Trained by [BAAI](https://huggingface.co/BAAI/bge-m3)** — production-grade, not a research toy

The backend speaks the OpenAI-compatible `/v1/embeddings` protocol via [SiliconFlow](https://siliconflow.cn) (free tier covers BGE-M3) — point `EMBEDDER_API_BASE_URL` at any OpenAI-compatible endpoint to swap providers. The [`BaseEmbedder`](sembr/embedder/base.py) abstract class defines the contract (`model_version`, `is_loaded`, `aembed(texts) -> list[list[float]]`) — community contributors can drop in a local backend (e.g. mlx-lm, Ollama) by subclassing it and registering in [`sembr/embedder/factory.py`](sembr/embedder/factory.py).

## Dashboard

Once `docker compose up --build` is healthy, browse to **[http://localhost:8000/dashboard](http://localhost:8000/dashboard)** for a read-only monitoring view: per-feed fetch outcomes (with 24h sparkline), embedder latency / failure counts, and pending / dead / Qdrant article counts with drill-down detail.

Set `DASHBOARD_TOKEN` in `.env` whenever the host is reachable beyond `localhost` — feed URLs and dead-article error messages are otherwise public. With a token set, the UI prompts for it once and then persists in the browser's `localStorage` + a path-scoped cookie. The JSON API at `/api/dashboard/*` accepts the same token via the `X-Dashboard-Token` header for scripting.

The bundled UI is plain HTML + Alpine.js + Chart.js with no Node toolchain. Disable it by removing `web/static/index.html`; the JSON API still serves at `/api/dashboard/*`. See [`web/README.md`](web/README.md) for details.

⚠️ **LAN exposure warning**: when no token is configured the dashboard shows a yellow banner — anyone on the LAN can read your feed configuration and recent fetch errors.

## Status

**1.0** — first stable release. Ships with:

- RSS ingestion + BGE-M3 embeddings via SiliconFlow API + Qdrant vector store
- Intent CRUD via REST API, with both cron-mode (preset + per-intent timezone) and event-mode (trigger after N matching articles or T seconds) schedules
- LLM-generated summaries via any OpenAI-compatible chat-completions endpoint (default: DeepSeek-V4-Flash on SiliconFlow)
- Email digest delivery via SMTP (per-intent timezone + matcher score badges)
- Read-only monitoring dashboard with live log SSE
- Runtime settings editor that writes the host `.env` and recreates the affected containers in place

Telegram / Discord / Slack channels and a local LLM backend are post-1.0 work — the [notifier](docs/modules/notifier.md) and [summarizer](docs/modules/summarizer.md) module docs explain the seams that make adding them additive rather than invasive.

## License

Apache-2.0

## Built by

[Peakstone Labs](https://github.com/Peakstone-Labs) — AI-native quantitative research.
