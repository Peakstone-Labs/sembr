# sembr

> Semantic news monitoring via reverse RAG.

**sembr** lets you write a natural-language intent once — _"monitor Fed policy impact on emerging-market currencies"_ — and receive matching news pushed to Telegram, Discord, or Slack as it arrives. No keyword lists, no hand-tuned filters.

## How it differs from traditional RAG

Traditional RAG: user query → search documents → answer.
**Reverse RAG (sembr)**: pre-stored intent vector → continuously scan incoming articles → push semantic matches.

You describe what you care about once. sembr does the watching.

## Quickstart

Requires Docker + Docker Compose. First image pull (`qdrant/qdrant:v1.17.1` ~250MB, `python:3.12` ~350MB) can take 5–10 minutes — `/health` returns 503 until both services are up, which is expected.

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
cp .env.example .env            # optional; defaults work for a local run
docker compose up --build

# in another shell, once both containers are running:
curl -i http://localhost:8000/health
# expected: HTTP/1.1 200 OK
# {"status":"ok","components":{"qdrant":"ok","sqlite":"ok","embedder":"not_loaded"}}
```

The `embedder` field is intentionally `"not_loaded"` in 0.1.0 — model loading lands in a later release and does not gate `/health`.

**Filesystem note**: keep `./data/` on a local POSIX-ish filesystem (ext4 / APFS / NTFS local). SQLite WAL is unsafe on network shares (NFS, SMB, virtio-9p).

**Port override**: `API_PORT=8080 docker compose up` exposes the API on `localhost:8080`. See `.env.example` for the full settings surface.

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
