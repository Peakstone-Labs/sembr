# Getting Started

!!! tip "Prefer an AI agent do this for you?"
    `agent/INSTALL.md` in the repo is a step-by-step that an LLM agent (Claude Code, Cursor, OpenClaw, Hermes, …) with shell access can run end-to-end — including hardware checks, parallel image pulls, API-key validation, and an opinionated default for the access-mode choice. Hand the URL [`agent/INSTALL.md`](https://github.com/Peakstone-Labs/sembr/blob/main/agent/INSTALL.md) to your agent and let it drive. The page below is the manual walk-through for when you want to do it yourself.

## Prerequisites

- Docker + Docker Compose
- A free [SiliconFlow](https://siliconflow.cn) API key (for BGE-M3 embeddings)

## 1. Clone and configure

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
cp .env.example .env
```

Open `.env` and set your SiliconFlow key:

```
EMBEDDER_API_KEY=sk-your-actual-key-here
```

`EMBEDDER_API_KEY` is the only required value. The container exits immediately at startup if it is missing or blank.

!!! warning "Default binding is your **LAN**, not just `localhost`"
    Out of the box, the API container publishes on `0.0.0.0:8000` — anyone on the same Wi-Fi / office network as this machine can already reach `http://<your-LAN-ip>:8000`. The Dashboard auth gate is **off** by default (empty `DASHBOARD_TOKEN`), so they could also POST `/intents`, change feed URLs, drain your SiliconFlow quota, and read your digests.

    Two knobs to set this right before step 2:

    - **localhost-only** (no LAN access): put `SEMBR_BIND_ADDR=127.0.0.1` in `.env`.
    - **LAN with auth** (you + trusted devices): leave bind unset, but set `DASHBOARD_TOKEN=$(openssl rand -hex 16)` in `.env`.

    For public-internet exposure see [Deployment / Public server](deployment/public.md) — there's a separate hardening checklist.

## 2. Start all services

```bash
docker compose up --build
```

First run pulls `qdrant/qdrant:v1.17.1` (~100 MB) and `diygod/rsshub:latest` (~300–400 MB), then builds the API image from `python:3.12` (~340 MB) with the Docker CLI apt packages (~150 MB) and pip wheels (~150 MB). Total network download is roughly **1 GB**; allow 10–15 minutes on a typical home connection.

## 3. Verify health

```bash
curl -i http://localhost:8000/health
```

If you set `SEMBR_HOST_PORT=8080` (or any other value — see [Port override](#port-override) at the bottom of this page), substitute that port in every URL below.

While the embedder probe is running you get:

```
HTTP/1.1 503 {"status":"degraded","components":{"embedder":"loading",...}}
```

Once the probe succeeds:

```
HTTP/1.1 200 {"status":"ok","components":{"embedder":"ok",...}}
```

## 4. Configure email delivery

Email is the only built-in channel today. Set the SMTP fields in `.env`:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=your-app-password
SMTP_FROM=you@example.com
```

Leaving `SMTP_HOST` empty disables email — the rest of the app still runs.

## 5. Create your first intent

If you set `DASHBOARD_TOKEN` in `.env` (recommended — see step 1's warning), every `/intents` / `/feeds` / `/api/*` request needs the `X-Dashboard-Token` header. The snippet below adapts automatically:

```bash
TOKEN=$(grep -E '^DASHBOARD_TOKEN=' .env | cut -d= -f2-)

curl -X POST http://localhost:8000/intents \
  -H "Content-Type: application/json" \
  ${TOKEN:+-H "X-Dashboard-Token: $TOKEN"} \
  -d '{
    "name": "fed-em-fx",
    "text": "Fed policy impact on emerging market currencies",
    "timezone": "America/New_York",
    "schedule": {"mode": "cron", "preset": "daily", "hour": 8, "minute": 0},
    "channels": [{"type": "email", "to": ["you@example.com"]}]
  }'
```

This intent fires every day at 08:00 in `America/New_York`. The digest renders timestamps in that same timezone. For event-mode delivery (fire after N matching articles, or T seconds since the first buffered match), use `"schedule": {"mode": "event", "trigger_count": 3, "max_wait_seconds": 1800}` instead.

## Dashboard

Browse to **http://localhost:8000/dashboard** for a monitoring view: per-feed fetch outcomes, embedder stats, and article pipeline counts.

!!! warning "Why `DASHBOARD_TOKEN` matters even on a single home network"
    With `DASHBOARD_TOKEN` empty (the default), every `/intents`, `/feeds`, `/api/prompts`, `/api/settings`, and `/api/external/*` endpoint is **unauthenticated**. Anyone who can reach `http://<this-host>:8000` can:

    - POST a new intent that emails them your digests
    - Edit feed URLs to point at an attacker-controlled server
    - Trigger fires that burn your SiliconFlow embedding / LLM quota
    - Change settings (which may include secrets in mask form)

    The default `0.0.0.0` bind means "anyone on the same Wi-Fi / office LAN" reaches this. Set `DASHBOARD_TOKEN=$(openssl rand -hex 16)` in `.env` and `docker compose restart api`. For public-internet deployment do **not** stop here — read [Deployment / Public server](deployment/public.md).

## Customising prompt templates

The dashboard's **Templates** tab (between Intents and Logs) is the runtime editor for the LLM prompts the summarizer uses. You can:

- **Duplicate** the bundled `default` template (read-only, in both `system/` and `instruction/`) and edit the copy
- **Rename** a template — the rename request atomically moves the file and updates every intent that references it
- **Delete** an unused template (a template referenced by an intent returns HTTP 409 listing the dependents)

Saves run a strict-placeholder dry-render: a typo like `{intent}` in an instruction template (allowed: `{intent_text}`, `{articles}`) is rejected with HTTP 422 before the bad bytes reach disk. Edits also work directly on disk under `./prompts/{system,instruction}/` — the bundled `docker-compose.yml` mounts `./prompts` read-write so the dashboard can write through, and the summarizer reads templates on every tick (no cache invalidation needed).

Per-file size cap is 64 KiB; the reserved name `default` is read-only on both kinds. See the README "Custom prompt templates" section for a complete CLI walkthrough.

## Data persistence

Feed list and article fingerprints are stored in `./data/sembr.db` (SQLite, bind-mounted from the host). They survive rebuilds and restarts. Only `rm -rf ./data/` permanently deletes them.

!!! note "Filesystem requirement"
    Keep `./data/` on a local POSIX-ish filesystem (ext4 / APFS / NTFS local). SQLite WAL mode is unsafe on network shares (NFS, SMB, virtio-9p).

## Port override

Set `SEMBR_HOST_PORT=8080` in `.env` to expose the API on `localhost:8080`. The in-container bind port is hardcoded to `8000` in the Dockerfile CMD.
