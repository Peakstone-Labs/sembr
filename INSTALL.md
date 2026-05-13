# sembr — Agent-Driven Install Guide

> **This document is written for an AI coding agent** (Claude Code, Cursor, Cline, Aider, Continue, Roo, or similar) running on the user's machine with shell access. The user has handed you this URL with the intent: "install sembr for me."
>
> A human can follow this guide too, but the structure — explicit phases, parallel work, "Tell user" / "Ask user" labels — is optimised for autonomous agent execution with minimal back-and-forth.

## How to use this document

Execute the phases below in order. Each phase has four parts:

| Block | Meaning |
| --- | --- |
| **Agent** | Shell commands you run yourself and inspect the output of |
| **Tell user** | Information you surface to the user (relay verbatim or in their language) |
| **Ask user** | A blocking question; wait for the user's reply before continuing |
| **Pass / On failure** | The condition that ends the phase, and what to do if it doesn't hold |

**Speak the user's language.** If the user has been chatting with you in Chinese, translate your "Tell user" / "Ask user" messages into Chinese (or whatever language they're using). The commands and code blocks stay as-is.

**Parallel work is a feature.** Some phases note "kick this off in the background and proceed." Honour it — the headline parallelism is *Docker pulls in the background while the user is fetching API keys*. Done well, the whole install fits in ~15 minutes of wall-clock time of which ~10 minutes is fully parallel.

**Idempotency.** Each phase begins with a check for "already done" and skips if so. Re-running this guide on a partially-installed machine should be safe.

**Consent before privileged action.** Never `sudo` or install system packages without surfacing exactly what you're about to do and getting the user's "ok". The user is in charge; you're the operator.

**What sembr is** (for context — don't relay this unless asked): a self-hosted **intent radar** built on "Reverse RAG". The user writes a natural-language *intent* once; sembr continuously scans RSS / NewsAPI / Twitter, vector-matches new articles to the intent, and emails an LLM-analyzed digest on schedule. Apache-2.0, runs as Docker Compose.

---

## Phase 0 — Acknowledge the plan with the user

**Tell user:**

> "I'll deploy sembr on this machine. Total time is about 15 minutes; about 10 of those are Docker image pulls running in the background, during which I'll have you grab a free API key in parallel. Proceeding."

No question to ask — just set expectations.

---

## Phase 1 — Hardware and environment self-check

**Agent:**

```bash
# OS + arch
uname -s    # Linux | Darwin | Windows_NT (under WSL)
uname -m    # x86_64 | arm64 | aarch64

# Free disk under the install target (default: ~/sembr)
df -h ~ | tail -1

# Available RAM (cross-platform best-effort)
( command -v free  >/dev/null && free -h ) \
 || ( command -v vm_stat >/dev/null && vm_stat | head -5 ) \
 || true

# Network reachability for the default model provider
curl -fsI -m 5 https://api.siliconflow.cn/v1/models >/dev/null && echo "siliconflow: ok" || echo "siliconflow: unreachable"
```

**Pass conditions:**
- OS is `Linux` or `Darwin` (macOS); Windows must be running under WSL2 (`uname -s` will report `Linux`)
- Arch is `x86_64` / `amd64` / `arm64` / `aarch64` — all are supported
- Free disk under `$HOME` ≥ **4 GB**
- Free RAM ≥ **2 GB** available, **4 GB total** preferred. The default stack reserves ~1.5 GB at idle (api ~125 MiB + rsshub ~355 MiB + qdrant ~520 MiB measured) and is capped at ~4 GB total via docker-compose `mem_limit`. Heavy workloads (millions of articles, tens of concurrent intents) may want 8 GB+.
- `siliconflow.cn` reachable

**On failure:**
- Insufficient disk / RAM → stop. Tell user what's short and what's required.
- `siliconflow.cn` unreachable → not fatal, but tell user: the default embedder + LLM both go through SiliconFlow. They may need to swap to a different OpenAI-compatible endpoint in Phase 4; flag this for now and continue.
- Native Windows (not WSL) → stop. Tell user to install WSL2 + Ubuntu, then re-run this guide from inside WSL.

---

## Phase 2 — Dependency check

sembr needs Docker (with Compose v2 plugin) and Git. Nothing else.

**Agent:**

```bash
docker --version                  # need 24.0+ or 25.0+
docker compose version             # need v2.x (the plugin, not the legacy docker-compose binary)
git --version
docker info >/dev/null 2>&1 && echo "daemon: ok" || echo "daemon: not running"
```

**Pass conditions:** all four commands return zero exit and a sensible version.

**On failure — Docker missing:**

| OS | Recommended install path |
| --- | --- |
| macOS (Apple Silicon or Intel) | Docker Desktop — `brew install --cask docker`, then launch the app once to grant permissions |
| Ubuntu / Debian | `docker.io` + `docker-compose-plugin` via apt: `sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin`, then `sudo usermod -aG docker $USER` and have the user re-login |
| Fedora / RHEL | `sudo dnf install -y docker docker-compose-plugin && sudo systemctl enable --now docker` |
| Arch | `sudo pacman -S docker docker-compose` |

**Ask user** before running any `sudo` / `brew` command:

> "Docker is not installed. I'd like to run: `<exact command>`. Proceed? (yes / no / I'll install it myself)"

If "no" or "I'll install it myself" — pause and tell the user how to install Docker themselves; wait for them to confirm it's running.

**On failure — Docker daemon not running:**
- macOS → tell user to launch Docker Desktop from /Applications
- Linux → `sudo systemctl start docker` (ask first)

**On failure — user not in `docker` group (Linux only):**
- `sudo usermod -aG docker $USER` and tell user they need to **log out and back in** for the group membership to apply. Re-run Phase 2 after re-login.

---

## Phase 3 — Clone, start parallel work, queue API-key fetch

**Agent:**

```bash
# Default install location. If ~/sembr already exists, ask user before overwriting.
test -d ~/sembr && echo "EXISTS" || echo "NEW"
```

If exists → **Ask user**: "`~/sembr` already exists. Use it as-is (assume previous install), use a different directory, or delete and re-clone?"

If new:

```bash
git clone https://github.com/Peakstone-Labs/sembr.git ~/sembr
cd ~/sembr
cp .env.example .env
```

**Pass condition:** `~/sembr/.env` exists (was copied from `.env.example`).

### Kick off the parallel work

The slow steps below take 5–10 minutes combined. Run them **in the background** so the user has time to fetch API keys.

```bash
cd ~/sembr
# Pull the two pre-built images
docker compose pull qdrant rsshub > /tmp/sembr-pull.log 2>&1 &
PULL_PID=$!
# Build the API image (Python base + Docker CLI apt + pip wheels via uv sync)
docker compose build api > /tmp/sembr-build.log 2>&1 &
BUILD_PID=$!
```

Remember `$PULL_PID` and `$BUILD_PID`; you'll wait on them in Phase 5.

### Tell user — parallel work begins now

**Tell user:**

> "Docker is pulling Qdrant + RSSHub (~500 MB) and building the sembr API image (~5 min) in the background.
>
> **While that runs, please grab API keys.** Only the first one is required; the rest are optional and you can skip them.
>
> 1. **SiliconFlow** (required) — free embeddings + cheap LLM.
>    https://siliconflow.cn → sign up → "API Keys" → "Create" → copy the `sk-...` value. **Paste it to me when ready.**
>
> 2. **NewsAPI.ai** (optional) — unlocks 30 pre-configured English news sources (Reuters, BBC, NYT, WSJ, FT, Economist, ...). Free token covers ~30 days of normal polling.
>    https://newsapi.ai → sign up → copy your API key.
>
> 3. **SMTP creds** (optional) — sembr's default delivery channel is email. If you want email digests, have ready: SMTP host (e.g. `smtp.gmail.com`), port (typically 587), username, app-password (NOT your account password — Gmail needs an app-password from Google Account → Security → App passwords), and the `From:` address. If you skip this, sembr still runs but won't email anyone — you can run intents manually via the API.
>
> 4. **Twitter `auth_token`** (optional) — only needed if you want the pre-seeded Elon Musk Twitter feed (or your own Twitter feeds) to work. To get it: log in to x.com → DevTools (F12) → Application → Cookies → `https://x.com` → copy the value of `auth_token` (40-char hex).
>
> Take your time — the install can wait."

### Ask user

**Ask user:** "Ready to share the SiliconFlow key? Paste it (or say 'skip' to use a non-SiliconFlow endpoint)."

Wait until the user replies with a key (`sk-...` shape) or "skip".

### Validate the key before writing it to disk

If user provided a SiliconFlow key:

```bash
curl -sf -m 10 -H "Authorization: Bearer <KEY>" https://api.siliconflow.cn/v1/models | head -c 200
```

**Pass condition:** HTTP 200, output is JSON with `"object":"list"`.

**On 401:** key is wrong. Tell user, ask for the key again.
**On timeout / connection error:** network problem. Tell user, suggest they check connectivity.

If user said "skip" — they intend to use a non-SiliconFlow endpoint. Ask them for the OpenAI-compatible base URL + key they want to use instead.

---

## Phase 4 — Configure `.env`

You should now have at least the SiliconFlow API key. Open `~/sembr/.env` and write the values. Use `sed` or a small Python one-liner — do **not** open an interactive editor on the user's behalf.

```bash
cd ~/sembr

# 1. SiliconFlow key (powers both embedder and LLM by default)
KEY='sk-...'   # the value the user gave you
# Escape any & or / in KEY for sed; the SiliconFlow keys are alphanumeric so plain replacement is safe.
sed -i.bak "s|^EMBEDDER_API_KEY=.*|EMBEDDER_API_KEY=${KEY}|" .env
sed -i.bak "s|^LLM_API_KEY=.*|LLM_API_KEY=${KEY}|" .env

# 2. Display timezone — default Asia/Shanghai. Set to the user's system timezone.
TZ=$(timedatectl show -p Timezone --value 2>/dev/null || readlink /etc/localtime | sed 's|.*/zoneinfo/||' || echo Asia/Shanghai)
sed -i.bak "s|^DISPLAY_TIMEZONE=.*|DISPLAY_TIMEZONE=${TZ}|" .env

# clean up sed backups
rm -f .env.bak
```

### Optional values — ask the user one at a time

For each of the following, **ask only if the user said they had it ready** in Phase 3. Don't pester.

**SMTP (email delivery)** — if user has creds, write all five:

```bash
# Ask user for: SMTP_HOST, SMTP_PORT (default 587), SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM
# Set them via sed as above. Leave SMTP_USE_STARTTLS=true and SMTP_USE_SSL=false at defaults unless user specifies otherwise.
```

**NewsAPI.ai key** — if user has it:

```bash
sed -i.bak "s|^NEWSAPI_API_KEY=.*|NEWSAPI_API_KEY=${NEWSAPI_KEY}|" .env && rm -f .env.bak
```

(If `NEWSAPI_API_KEY=` isn't in `.env.example`, append it: `echo "NEWSAPI_API_KEY=${NEWSAPI_KEY}" >> .env`.)

**Twitter `auth_token`** — if user has it:

```bash
sed -i.bak "s|^TWITTER_AUTH_TOKEN=.*|TWITTER_AUTH_TOKEN=${TWITTER_TOKEN}|" .env && rm -f .env.bak
```

**`DASHBOARD_TOKEN`** — if the user plans to expose the host beyond `localhost` (LAN, VPS, public IP):

```bash
TOKEN=$(openssl rand -hex 16)
sed -i.bak "s|^DASHBOARD_TOKEN=.*|DASHBOARD_TOKEN=${TOKEN}|" .env && rm -f .env.bak
```

**Tell user (only if you generated a DASHBOARD_TOKEN):**

> "Generated `DASHBOARD_TOKEN=<value>` — write this down. You'll need it to log in to the dashboard. The token is also stored in `.env` on this machine."

### Ask user — anything else they want

**Ask user:** "Anything else to configure right now? (Common: change `LLM_MODEL` to a non-SiliconFlow endpoint, change Qdrant or SQLite paths, set `SEMBR_HOST_PORT` if 8000 is taken). If unsure, say 'no' and we'll move on."

If 'no' — proceed to Phase 5.

---

## Phase 5 — Bring up and verify

### Wait for the background work

```bash
# Wait for pull + build (started in Phase 3). If either failed, surface the log.
wait $PULL_PID  || ( echo "pull failed:" && tail -30 /tmp/sembr-pull.log && exit 1 )
wait $BUILD_PID || ( echo "build failed:" && tail -30 /tmp/sembr-build.log && exit 1 )
```

**On failure during pull/build:** read the tail of the log, surface the salient error to the user, and ask whether to retry or abort. Common: transient network → retry usually works; `apt-get` mirror down inside Dockerfile → retry in 5 min.

### Start the stack

```bash
cd ~/sembr
docker compose up -d
```

### Poll `/health` until ready

```bash
PORT=$(grep -E '^SEMBR_HOST_PORT=' .env | cut -d= -f2)
PORT=${PORT:-8000}

for i in $(seq 1 60); do
  if curl -fsm 3 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    curl -s "http://localhost:${PORT}/health"
    echo " ✓ healthy"
    break
  fi
  echo "  ... still warming up (attempt $i/60)"
  sleep 10
done
```

**Pass condition:** `/health` returns HTTP 200 with JSON body `{"status":"ok",...}`.

**On 503 with `"embedder":"loading"`:** the embedder probe is still running. Continue polling — first probe can take 30–60 s.

**On 503 with `"embedder":"failed"`:** the SiliconFlow probe is failing. Most likely the API key is wrong, or the key works but the model isn't enabled on the user's account. Run:

```bash
docker compose logs api | grep -i embedder | tail -20
```

Surface the error to the user, ask for a corrected key, re-run Phase 4 for the key, then `docker compose restart api`.

**On the loop exiting without 200:** print `docker compose ps` + `docker compose logs --tail=50 api`; surface to user.

---

## Phase 6 — First intent (recommended)

This is the "did it actually work" moment. Encourage the user to do it, but don't insist.

**Ask user:** "Want to create your first intent now? Give me a one-sentence brief — what do you want sembr to monitor? Examples: 'Fed policy impact on emerging-market currencies', '中国半导体产业政策动态', 'OpenAI / Anthropic / DeepMind product releases'. Say 'skip' to do it later via the dashboard."

If user gives a brief:

```bash
# Use the email address they configured in SMTP_FROM (or ask if they want a different recipient)
# Use the user's system timezone (from Phase 4)
INTENT_TEXT='<user brief>'
RECIPIENT='<user email>'
TZ='<system tz>'
PORT=${PORT:-8000}

curl -X POST "http://localhost:${PORT}/intents" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"first\",
    \"text\": \"${INTENT_TEXT}\",
    \"timezone\": \"${TZ}\",
    \"schedule\": {\"mode\": \"cron\", \"preset\": \"daily\", \"hour\": 9, \"minute\": 0},
    \"channels\": [{\"type\": \"email\", \"to\": [\"${RECIPIENT}\"]}]
  }"
```

Expected response: HTTP 201 with the created intent's JSON, including its assigned `id`.

If the user didn't configure SMTP, skip the `channels` field or use `[]` so the intent stores but won't try to email — the user can edit channels later via the dashboard.

---

## Done — final summary to user

**Tell user:**

> "sembr is running.
>
> - **Dashboard**: http://localhost:${PORT}/dashboard {if DASHBOARD_TOKEN set: '— login with the token I generated earlier'}
> - **API**: http://localhost:${PORT}
> - **Health**: http://localhost:${PORT}/health
> - **Data**: ~/sembr/data/ (SQLite + Qdrant storage — back this up)
> - **Logs**: `docker compose logs -f api` from `~/sembr`
> - **Stop / start**: `docker compose down` / `docker compose up -d` from `~/sembr`
>
> 53 pre-loaded sources are already pulling in the background. Your first digest fires at the scheduled time. Add more intents from the dashboard or via `POST /intents`. Documentation: https://peakstone-labs.github.io/sembr"

---

## Troubleshooting matrix

Use this if any phase fails or the user reports a problem later.

| Symptom | Most likely cause | Fix |
| --- | --- | --- |
| `/health` 503, `"embedder":"loading"` | Embedder probe still warming | Wait 30–60 s and re-poll |
| `/health` 503, `"embedder":"failed"` | Wrong API key / model not enabled on account / network blocks SiliconFlow | Curl SiliconFlow `/v1/models` with the key to diagnose. Fix key, `docker compose restart api`. |
| `/health` 503, `"qdrant":"unhealthy"` | Qdrant container crashed | `docker compose logs qdrant --tail=50` — usually OOM (raise mem_limit) or storage perm issue under `./data/qdrant` |
| Port 8000 already in use | Another service | Set `SEMBR_HOST_PORT=8080` (or any free port) in `.env`, then `docker compose up -d` |
| `docker compose build` fails on `uv sync` | Transient PyPI / SiliconFlow network | Retry. If persistent, ensure host has open egress for `pypi.org` |
| `docker compose up` says "permission denied on /var/run/docker.sock" | User not in docker group (Linux) | `sudo usermod -aG docker $USER` and re-login |
| Settings tab in dashboard returns 500 | Docker socket bind-mount missing or read-only | Verify `docker-compose.yml` mounts `/var/run/docker.sock` and the docker group is correct |
| RSSHub feeds all 503 | RSSHub container crashed or rate-limited by source | `docker compose logs rsshub --tail=50`; restart: `docker compose restart rsshub` |
| Twitter feeds empty | `TWITTER_AUTH_TOKEN` not set or token expired | Refresh the cookie from x.com, update `.env`, `docker compose restart rsshub` |
| NewsAPI feeds empty | `NEWSAPI_API_KEY` not set, or free-tier quota exhausted | Check `docker compose logs api | grep -i newsapi`; if quota burn is the cause, raise `NEWSAPI_POLL_INTERVAL_MINUTES` or remove some NewsAPI feeds |
| Filesystem warnings in logs | `./data` on a network share (NFS / SMB / virtio-9p) | Move `~/sembr` to a local-disk path; SQLite WAL is unsafe on network shares |

---

## What NOT to do (agent guardrails)

- **Don't** modify code under `sembr/` or rewrite `docker-compose.yml`. This is the user's deployment, not a dev install. Configuration belongs in `.env` and runtime overrides — never in committed code.
- **Don't** install Python packages, run `uv sync`, or run `pytest` on the host. Everything runs inside Docker.
- **Don't** run `git pull` after the initial clone. Leave the user at the launch tag.
- **Don't** publish or expose the dashboard to the public internet without setting `DASHBOARD_TOKEN` and reading `docs/deployment/public.md`. The dashboard editor is effectively root on the host via the Docker socket mount.
- **Don't** commit the user's `.env` to any repo. It contains their API keys.
- **Don't** delete `~/sembr/data/` to "clean up" — that's where the SQLite DB and Qdrant vectors live. Confirm before any destructive operation.
- **Don't** invent endpoints or env vars that aren't in `.env.example` / the docs. If a setting isn't documented, surface that to the user rather than guessing.
- **Don't** silently change `LLM_MAX_PROMPT_CHARS` or other tuning knobs without telling the user — defaults are sensible for the default LLM (DeepSeek-V4-Flash with 1 M context).

---

## Reference — the env-var surface at a glance

For your scanning convenience. Full descriptions are in `.env.example`.

| Variable | Required? | Default | Notes |
| --- | --- | --- | --- |
| `EMBEDDER_API_KEY` | **required** | — | SiliconFlow (or any OpenAI-compatible `/v1/embeddings`) |
| `EMBEDDER_API_BASE_URL` | optional | `https://api.siliconflow.cn/v1` | Swap provider |
| `EMBEDDER_MODEL` | optional | `BAAI/bge-m3` | 1024-dim, 8192-token ctx |
| `LLM_API_KEY` | **required** | — | Usually reuse `EMBEDDER_API_KEY` |
| `LLM_API_BASE_URL` | optional | `https://api.siliconflow.cn/v1` | |
| `LLM_MODEL` | optional | `deepseek-ai/DeepSeek-V4-Flash` | Any OpenAI-compatible chat model |
| `LLM_MAX_PROMPT_CHARS` | optional | `1500000` | Match to model context window |
| `SMTP_HOST` | optional | empty | Empty disables email channel |
| `SMTP_PORT` | optional | `587` | |
| `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM` | required if `SMTP_HOST` set | — | App-password, not account password, for Gmail |
| `DISPLAY_TIMEZONE` | optional | `Asia/Shanghai` | IANA tz for digest rendering |
| `NEWSAPI_API_KEY` | optional | empty | Enables 30 NewsAPI.ai sources |
| `NEWSAPI_POLL_INTERVAL_MINUTES` | optional | `30` | One token per poll across all NewsAPI feeds |
| `TWITTER_AUTH_TOKEN` | optional | empty | `auth_token` cookie value, 40-char hex |
| `DASHBOARD_TOKEN` | conditional | empty | **Must set** if exposing beyond localhost |
| `SEMBR_HOST_PORT` | optional | `8000` | Override if 8000 is in use |

---

## Versioning

This guide tracks sembr `main`. For a specific version, prefix the URL with the tag: `https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/INSTALL.md`.
