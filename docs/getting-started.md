# Getting Started

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

## 2. Start all services

```bash
docker compose up --build
```

First run pulls `qdrant/qdrant:v1.17.1` (~100 MB) and `diygod/rsshub:latest` (~300–400 MB), then builds the API image from `python:3.12` (~340 MB) with the Docker CLI apt packages (~150 MB) and pip wheels (~150 MB). Total network download is roughly **1 GB**; allow 10–15 minutes on a typical home connection.

## 3. Verify health

```bash
curl -i http://localhost:8000/health
```

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

```bash
curl -X POST http://localhost:8000/intents \
  -H "Content-Type: application/json" \
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

!!! warning "Security"
    Set `DASHBOARD_TOKEN` in `.env` before exposing the port beyond `localhost`. Without it, feed URLs and error messages are readable by anyone on the network.

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
