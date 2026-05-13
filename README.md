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

**Port override**: set `SEMBR_HOST_PORT=8080` in `.env` (or as a shell env var) to expose the API on `localhost:8080`. The in-container bind port is hardcoded to `8000` in the Dockerfile CMD. See [docs/configuration.md](docs/configuration.md) for the full settings surface. Planning to put sembr on a public IP? Read [docs/deployment/public.md](docs/deployment/public.md) first.

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
  must expose it publicly, follow [docs/deployment/public.md](docs/deployment/public.md)
  — TL;DR keep the default `127.0.0.1` bind, put sembr behind a reverse proxy
  with TLS, and set a strong `DASHBOARD_TOKEN`.
- **`cp .env.example .env` BEFORE the first `docker compose up`** — without an
  existing host `.env` file, Docker creates a directory at the bind-mount path
  and the API will refuse to start.

If you cannot accept the docker-socket exposure, run sembr without the Settings
tab: comment out the two volume mounts above; `/api/settings/save` will return
500 on RSSHub restart but the rest of the app keeps working.

## RSS Feeds

sembr comes with 23 pre-loaded free RSS sources, curated for substantive body text or information-dense headlines. They start collecting on first launch — no configuration needed.

### Pre-loaded sources

| Category | Sources |
| -------- | ------- |
| International news | The Guardian World, SCMP, NPR News, Washington Post, New Yorker |
| International finance | Bloomberg Markets |
| Chinese finance (long-form, via RSSHub) | 华尔街见闻, 第一财经, 第一财经-头条, 36氪, 虎嗅, 格隆汇热门文章, 东财-策略报告 |
| Chinese finance (newsflash, via RSSHub) | 财联社电报, 格隆汇快讯, 金十-快讯 |
| Chinese general (via RSSHub) | 澎湃新闻 |
| Government / Statistics (via RSSHub) | 国家统计局 |
| Academic (via RSSHub) | Nature, Nature Biotechnology, Nature Neuroscience |
| Tools / Open Source (via RSSHub) | HelloGitHub |
| Twitter (via RSSHub) | Elon Musk |

Sources marked "via RSSHub" route through the bundled [RSSHub](https://rsshub.app/) sidecar (`rsshub:1200`), which starts automatically alongside the API.

Twitter feeds additionally require a `TWITTER_AUTH_TOKEN` cookie value in `.env` — without it the route returns empty. Get the token from `x.com` DevTools → Application → Cookies → `auth_token` (40-char hex). See `.env.example` for details. The pre-loaded Elon Musk feed will sit idle until the token is set; you can also delete it via `DELETE /feeds/{id}` if you don't need Twitter sources.

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

sembr ships with two built-in templates (`prompts/system/default.md` and `prompts/instruction/default.md`). Manage them either through the **Templates tab in the dashboard** (Intents → Templates → +) or directly on disk under `./prompts/`. Both paths share the same files and validation rules.

### Dashboard (recommended)

Open `/dashboard`, click **Templates**. Two columns (system / instruction) list every template with a reference count showing which intents use each one. The toolbar lets you:

- **+ New System / Instruction Template** — seed from `default` (or use **Duplicate** on any row to seed from that one)
- **Edit** — inline editor with strict placeholder validation; save → `PUT /api/prompts/templates/{kind}/{name}`
- **Rename** — atomic file move + cascade UPDATE to every referencing intent in one request
- **Delete** — blocked (HTTP 409) if any intent references the template; click into the listed intents to detach first

The reserved name `default` is read-only on every write path (HTTP 403). Per-file size cap is 64 KiB; empty content is rejected (HTTP 422). The server runs a strict-placeholder dry-render on every save so a typo like `{intent}` in an instruction template fails before it can poison the next digest.

### Bare disk (CLI / scripting)

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
- `instruction` templates: allowed placeholders `{intent_text}` and `{articles}` (none are required, but unknown ones reject the save)
- `system` templates: allowed placeholder `{language}` (also optional)
- Edits to files on the host take effect on the **next tick** — no restart needed
- API surface: `GET /api/prompts/templates` (rich list with `ref_intents`/`is_builtin`), `GET /api/prompts/templates/{kind}/{name}` (preview), plus `POST/PUT/DELETE/POST-rename` for write paths

### Production deployment

The bundled `docker-compose.yml` bind-mounts `./prompts` as **read-write** so the dashboard can write to it. The bundled image runs as root (no `USER` directive) so the container can always write the host directory. If you switch to a non-root image, ensure the host `./prompts` directory is writable by the container UID — otherwise saves return HTTP 500 with a filesystem error in the logs. Templates are configuration-as-data: snapshot `./prompts/` along with `./data/` and the `.env` file when backing up.

## Embedder

sembr ships with **BGE-M3** as its default embedding model — and on [SiliconFlow](https://siliconflow.cn) it's **free to use**. Unlimited semantic monitoring with a top-tier multilingual model at zero inference cost.

**Why BGE-M3?** It's currently the best open-source embedder for Chinese + English mixed news content:

- **1024 dimensions, 8192-token context** — handles full article bodies, not just titles
- **Native bilingual** (CN/EN, plus 100+ languages) — critical for sembr's mixed RSS sources (财联社/Bloomberg/SCMP/Al Jazeera all in one intent)
- **MTEB top-tier on retrieval** — what reverse-RAG actually needs (intent vector ↔ article vector ANN match)
- **Trained by [BAAI](https://huggingface.co/BAAI/bge-m3)** — production-grade, not a research toy

The backend speaks the OpenAI-compatible `/v1/embeddings` protocol via [SiliconFlow](https://siliconflow.cn) (free tier covers BGE-M3) — point `EMBEDDER_API_BASE_URL` at any OpenAI-compatible endpoint to swap providers. The [`BaseEmbedder`](sembr/embedder/base.py) abstract class defines the contract (`model_version`, `is_loaded`, `aembed(texts) -> list[list[float]]`) — community contributors can drop in a local backend (e.g. mlx-lm, Ollama) by subclassing it and registering in [`sembr/embedder/factory.py`](sembr/embedder/factory.py).

## Dashboard

Once `docker compose up --build` is healthy, browse to **[http://localhost:8000/dashboard](http://localhost:8000/dashboard)** for a monitoring view: per-feed fetch outcomes (with 24h sparkline), embedder latency / failure counts, pending / dead / Qdrant article counts with drill-down detail (the qdrant view supports filter by ingest-date range, feed source, and title fuzzy-match), and per-container CPU / memory / uptime with a live 10-minute sparkline plus a one-click api+rsshub restart button.

Set `DASHBOARD_TOKEN` in `.env` whenever the host is reachable beyond `localhost` — feed URLs and dead-article error messages are otherwise public. With a token set, the UI prompts for it once and then persists in the browser's `localStorage` + a path-scoped cookie. The JSON API at `/api/dashboard/*` accepts the same token via the `X-Dashboard-Token` header for scripting.

The bundled UI is plain HTML + Alpine.js + Chart.js with no Node toolchain. Disable it by removing `web/static/index.html`; the JSON API still serves at `/api/dashboard/*`. See [`web/README.md`](web/README.md) for details.

⚠️ **LAN exposure warning**: when no token is configured the dashboard shows a yellow banner — anyone on the LAN can read your feed configuration and recent fetch errors.

## Status

**1.0** — first stable release. Ships with:

- RSS ingestion + BGE-M3 embeddings via SiliconFlow API + Qdrant vector store
- Intent CRUD via REST API, with both cron-mode (preset + per-intent timezone) and event-mode (trigger after N matching articles or T seconds) schedules
- LLM-generated summaries via any OpenAI-compatible chat-completions endpoint (default: DeepSeek-V4-Flash on SiliconFlow)
- Email digest delivery via SMTP (per-intent timezone + matcher score badges)
- Monitoring dashboard with live log SSE, per-container CPU / memory / uptime sparklines, qdrant article filter (date / source / title), and a token-gated api+rsshub restart trigger
- Runtime settings editor that writes the host `.env` and recreates the affected containers in place

Telegram / Discord / Slack channels and a local LLM backend are post-1.0 work — the [notifier](docs/modules/notifier.md) and [summarizer](docs/modules/summarizer.md) module docs explain the seams that make adding them additive rather than invasive.

## License

Apache-2.0

## Built by

[Peakstone Labs](https://github.com/Peakstone-Labs) — AI-native quantitative research.
