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

First run pulls `qdrant/qdrant:v1.17.1` (~100 MB) and `python:3.12` (~140 MB) — allow 5–10 minutes on a slow connection.

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

## 4. Create your first intent

```bash
curl -X POST http://localhost:8000/intents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "fed-em-fx",
    "text": "Fed policy impact on emerging market currencies",
    "channels": [{"type": "telegram", "chat_id": "@yourchannel"}]
  }'
```

The matcher runs every 5 minutes. Matched articles arrive as push notifications.

## Dashboard

Browse to **http://localhost:8000/dashboard** for a monitoring view: per-feed fetch outcomes, embedder stats, and article pipeline counts.

!!! warning "Security"
    Set `DASHBOARD_TOKEN` in `.env` before exposing the port beyond `localhost`. Without it, feed URLs and error messages are readable by anyone on the network.

## Data persistence

Feed list and article fingerprints are stored in `./data/sembr.db` (SQLite, bind-mounted from the host). They survive rebuilds and restarts. Only `rm -rf ./data/` permanently deletes them.

!!! note "Filesystem requirement"
    Keep `./data/` on a local POSIX-ish filesystem (ext4 / APFS / NTFS local). SQLite WAL mode is unsafe on network shares (NFS, SMB, virtio-9p).

## Port override

Set `SEMBR_HOST_PORT=8080` in `.env` to expose the API on `localhost:8080`. `API_PORT` controls the in-container bind port and should stay at `8000`.
