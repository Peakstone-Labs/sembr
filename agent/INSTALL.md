# sembr — Agent-Driven Install Guide

> **This document is written for an AI agent** (OpenClaw, Hermes or similar) running on the user's machine with shell access. The user has handed you this URL with the intent: "install sembr for me."
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

# Free disk under the install target. Defaults to $HOME — if the user picks a
# different SEMBR_DIR in Phase 3 (e.g. /srv/sembr, /data/...), re-check df
# against that filesystem before kicking off the parallel pull/build.
df -h ~ | tail -1

# Available RAM (cross-platform best-effort)
( command -v free  >/dev/null && free -h ) \
 || ( command -v vm_stat >/dev/null && vm_stat | head -5 ) \
 || true

# Network reachability for the default model provider.
# NOTE: we have no API key yet, so any HTTP response (200 / 401 / 403) means
# DNS + TLS + routing all work. Only "000" (curl could not get any response)
# counts as unreachable. Do NOT use `curl -f` here — it treats 401 as failure
# and produces a false negative against the unauthenticated /v1/models endpoint.
code=$(curl -s -o /dev/null -m 5 -w "%{http_code}" https://api.siliconflow.cn/v1/models)
[ "$code" != "000" ] && echo "siliconflow: reachable (HTTP $code)" || echo "siliconflow: unreachable"
```

**Pass conditions:**
- OS is `Linux` or `Darwin` (macOS); Windows must be running under WSL2 (`uname -s` will report `Linux`)
- Arch is `x86_64` / `amd64` / `arm64` / `aarch64` — all are supported
- Free disk under `$HOME` ≥ **4 GB**
- Free RAM ≥ **2 GB** available, **4 GB total** preferred. The default stack reserves ~1.5 GB at idle (api ~125 MiB + rsshub ~355 MiB + qdrant ~520 MiB measured) and is capped at ~4 GB total via docker-compose `mem_limit`. Heavy workloads (millions of articles, tens of concurrent intents) may want 8 GB+.
- `siliconflow.cn` reachable (HTTP 200 / 401 / 403 all count — auth happens later in Phase 3)

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

> ⚠️ **Critical: do NOT install the legacy `docker-compose` (v1) package.** It is unmaintained, conflicts with the v2 plugin syntax sembr's `docker-compose.yml` expects, and is no longer in Ubuntu 24.04+. sembr requires **Docker Compose v2** (`docker compose`, two words, plugin form). On Ubuntu / Debian this **only** comes from Docker's official apt repo — the distro's `docker.io` package does **not** bundle the compose plugin, and `apt install docker-compose-plugin` against the default sources will fail with "Unable to locate package". You must add `download.docker.com` first.

| OS | Recommended install path |
| --- | --- |
| macOS (Apple Silicon or Intel) | Docker Desktop — `brew install --cask docker`, then launch the app once to grant permissions. Compose v2 ships inside Desktop. |
| Ubuntu / Debian | Add Docker's official apt repo, then install `docker-ce` + `docker-compose-plugin`. Use the official one-liner below. |
| Fedora / RHEL | `sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin && sudo systemctl enable --now docker` (after adding `docker-ce.repo` — see below) |
| Arch | `sudo pacman -S docker docker-compose` (Arch's `docker-compose` package *is* v2 — exception to the rule above) |

**Ubuntu / Debian — full sequence (run as one block after confirming with the user):**

```bash
# 1. Prereqs + GPG key for Docker's apt repo
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/$(. /etc/os-release; echo "$ID")/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# 2. Add the repo (auto-detects ubuntu vs debian and the codename)
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/$(. /etc/os-release; echo "$ID") \
  $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 3. Install Engine + CLI + Compose plugin (NOT the legacy docker-compose package)
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 4. Allow current user to talk to the daemon without sudo
sudo usermod -aG docker $USER
# User must log out + back in (or run `newgrp docker` in a new shell) for the group change to take effect.
```

**Fedora / RHEL — repo first:**

```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo  # or .../rhel/...
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

**Ask user** before running any `sudo` / `brew` command:

> "Docker is not installed. I'd like to run the official Docker apt-repo setup + install `docker-ce` and `docker-compose-plugin` (the v2 plugin form `docker compose`, NOT the deprecated v1 `docker-compose` binary). Exact commands shown above. Proceed? (yes / no / I'll install it myself)"

If "no" or "I'll install it myself" — pause and tell the user how to install Docker themselves; wait for them to confirm it's running. **If they ask why not just `apt install docker.io docker-compose`**: docker.io lags upstream and `docker-compose` is v1 (Python, EOL'd 2023, syntax-incompatible with v2). sembr's compose file uses v2-only features.

**If you (the agent) already installed legacy `docker-compose` by mistake**, undo it before continuing:

```bash
sudo apt-get remove -y docker-compose          # remove v1
docker compose version                          # confirm the plugin reports v2.x
```

**On failure — Docker daemon not running:**

- macOS → tell user to launch Docker Desktop from /Applications.
- Linux (systemd) → `sudo systemctl start docker` (ask first).
- Linux (no systemd — WSL2, container, minimal VM) → start dockerd in the background, then **poll** until it actually answers. Don't use a fixed `sleep` — dockerd cold-start on a slow VM can exceed any number you'd pick.

  ```bash
  sudo nohup dockerd > /tmp/dockerd.log 2>&1 &
  disown
  for i in $(seq 1 30); do
    sudo docker info >/dev/null 2>&1 && break
    sleep 1
  done
  sudo docker info >/dev/null 2>&1 || { echo "dockerd failed to start; see /tmp/dockerd.log"; exit 1; }
  ```

  Then set up the sudo-prefix convention (see below).

**On failure — user not in `docker` group (Linux only):**

- `sudo usermod -aG docker $USER`. Group membership won't apply until next login, but the user does NOT need to stop and re-login — set up the sudo-prefix convention below.

### sudo-prefix convention (used by both fallbacks above)

If either fallback fires, every subsequent `docker` / `docker compose` command in Phases 3 / 5 / 6 must run with `sudo`. To make this mechanical, set a shell variable **once** here and use it everywhere:

```bash
# Default: no sudo needed (regular daemon, user in docker group).
DC="docker compose"
# If we just had to start dockerd manually OR the user isn't in the docker group yet:
if ! docker info >/dev/null 2>&1; then DC="sudo docker compose"; fi
echo "compose CLI: ${DC}"
```

**For the rest of this guide**, every `docker compose ...` command shown below is shorthand for `${DC} ...`. If the variable resolved to `sudo docker compose`, substitute accordingly — including the Phase 3 background pulls. **Tell the user:** "This environment needs `sudo` for `docker compose` for the rest of the install (no systemd, or you're not in the `docker` group yet). The install works identically; only the spelling differs."

---

## Phase 3 — Clone, start parallel work, queue API-key fetch

### Ask user — where to install

**Ask user:** "Where should I install sembr? The default is `~/sembr`. Press enter / say 'default' to use it, or give me a different absolute path (e.g. `/srv/sembr`, `~/projects/sembr`, `/data/apps/sembr`). The directory will hold the source tree, `.env`, and — most importantly — `data/` with the SQLite DB and Qdrant vectors, so pick a disk with room to grow."

Capture the answer into a shell variable that **persists for the rest of this guide**. Every subsequent shell command in Phases 3–6 that mentions `~/sembr` should be substituted with this path. Expand `~` yourself before storing (don't pass a literal `~` into commands that may run under contexts where it isn't expanded):

```bash
# Replace the right-hand side with the user's answer.
# Examples:
#   SEMBR_DIR="${HOME}/sembr"         # default
#   SEMBR_DIR="/srv/sembr"            # explicit absolute path
#   SEMBR_DIR="${HOME}/projects/sembr"
SEMBR_DIR="${HOME}/sembr"

# Sanity: must be absolute and the parent must exist and be writable.
case "${SEMBR_DIR}" in
  /*) : ;;
  *) echo "ERROR: SEMBR_DIR must be an absolute path" >&2; exit 1 ;;
esac
PARENT=$(dirname "${SEMBR_DIR}")
[ -d "${PARENT}" ] && [ -w "${PARENT}" ] || { echo "ERROR: ${PARENT} is not a writable directory" >&2; exit 1; }
echo "install target: ${SEMBR_DIR}"
```

**On non-writable parent / not-absolute path:** tell the user, ask again. Don't `sudo mkdir` someone's path for them without permission — that mints a root-owned directory and bites later.

**Agent:**

```bash
# If the target already exists, ask the user before overwriting.
test -d "${SEMBR_DIR}" && echo "EXISTS" || echo "NEW"
```

If exists → **Ask user**: "`${SEMBR_DIR}` already exists. Use it as-is (assume previous install), use a different directory, or delete and re-clone?"

If new:

```bash
git clone https://github.com/Peakstone-Labs/sembr.git "${SEMBR_DIR}"
cd "${SEMBR_DIR}"
cp .env.example .env
```

**Pass condition:** `${SEMBR_DIR}/.env` exists (was copied from `.env.example`).

### Kick off the parallel work

The slow steps below take 5–10 minutes combined. Run them **in the background** so the user has time to fetch API keys.

```bash
cd "${SEMBR_DIR}"
# Pull the two pre-built images
docker compose pull qdrant rsshub > /tmp/sembr-pull.log 2>&1 &
echo $! > /tmp/sembr-pull.pid
# Build the API image (Python base + Docker CLI apt + pip wheels via uv sync)
docker compose build api > /tmp/sembr-build.log 2>&1 &
echo $! > /tmp/sembr-build.pid
```

The PIDs go to `/tmp/sembr-*.pid` so Phase 5 can `wait` on them even if you've opened a fresh shell between phases (a daemon restart in Phase 2, a reconnect, etc.). Remember `$SEMBR_DIR` — re-export it after any shell restart.

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

A working SiliconFlow account is **not** enough — sembr needs **two specific models** enabled on it:
- `BAAI/bge-m3` (embedder, 1024-dim) — usually free tier
- `deepseek-ai/DeepSeek-V4-Flash` (LLM) — usually free tier, but can be regionally gated

Listing `/v1/models` only proves the key authenticates, not that these two models are reachable. Test each with a tiny real call.

If user provided a SiliconFlow key:

```bash
KEY='sk-...'   # the value the user gave you

# (1) Key is valid + account is active
echo "→ checking key auth …"
curl -sf -m 10 -H "Authorization: Bearer ${KEY}" \
  https://api.siliconflow.cn/v1/models | head -c 200 \
  || { echo "AUTH FAILED"; exit 1; }
echo

# (2) Embedding model is enabled and returns a 1024-dim vector
echo "→ probing BAAI/bge-m3 …"
EMBED_RESP=$(curl -s -m 15 -H "Authorization: Bearer ${KEY}" \
  -H "Content-Type: application/json" \
  -X POST https://api.siliconflow.cn/v1/embeddings \
  -d '{"model":"BAAI/bge-m3","input":"sembr install probe"}')
echo "${EMBED_RESP}" | head -c 200; echo
echo "${EMBED_RESP}" | grep -q '"embedding"' && echo "  ✓ embedder OK" \
  || echo "  ✗ embedder FAILED — see response above"

# (3) LLM is enabled and returns a chat completion
echo "→ probing deepseek-ai/DeepSeek-V4-Flash …"
LLM_RESP=$(curl -s -m 20 -H "Authorization: Bearer ${KEY}" \
  -H "Content-Type: application/json" \
  -X POST https://api.siliconflow.cn/v1/chat/completions \
  -d '{"model":"deepseek-ai/DeepSeek-V4-Flash","messages":[{"role":"user","content":"reply with the single word: pong"}],"max_tokens":8}')
echo "${LLM_RESP}" | head -c 300; echo
echo "${LLM_RESP}" | grep -q '"choices"' && echo "  ✓ LLM OK" \
  || echo "  ✗ LLM FAILED — see response above"
```

**Pass conditions (all three must hold):**
1. Step (1) returns JSON with `"object":"list"` — key authenticates.
2. Step (2) response contains `"embedding"` and the vector array is non-empty — embedder works.
3. Step (3) response contains `"choices"` with a non-empty `message.content` — LLM works.

**On failure — diagnose by which step failed:**

| Failing step | Most likely cause | Action |
| --- | --- | --- |
| (1) HTTP 401 | Key is wrong or revoked | Ask user for the key again. |
| (1) timeout / 000 | Network blocks SiliconFlow | Suggest VPN / proxy / swap to a different OpenAI-compatible provider (see "skip" branch below). |
| (2) `model_not_found` / 404 / 403 | `BAAI/bge-m3` not enabled on the account | Tell user to enable it in the SiliconFlow console (Models → search "bge-m3" → Enable). Free tier — no payment needed. Retry after enabling. |
| (3) `model_not_found` / 404 / 403 | `deepseek-ai/DeepSeek-V4-Flash` not enabled | Same fix on the console. If the model has been retired or renamed by SiliconFlow, ask the user which LLM they want to use instead and adjust `LLM_MODEL` in Phase 4. |
| (2) or (3) returns `"insufficient_quota"` / `"rate_limit"` | Account out of credit or rate-limited | Tell user; suggest topping up or waiting. sembr won't function until at least the embedder works (the LLM is only invoked at digest time, so step 3 failures are non-blocking for *boot* but will silently break digests). |

If user said **"skip"** — they intend to use a non-SiliconFlow endpoint. Ask for:
- OpenAI-compatible **base URL** (e.g. `https://api.deepseek.com/v1`, `http://localhost:11434/v1` for Ollama, etc.)
- **API key** for that endpoint
- The **embedding model name** they want (must produce ≥1024-dim vectors, or you'll need to alter `EMBEDDER_MODEL` + understand the dimension implications for Qdrant collection layout — easiest is to stick with a `bge-m3`-equivalent)
- The **chat model name** for `LLM_MODEL`

Re-run the three-step probe above against their endpoint and model names before proceeding.

---

## Phase 4 — Configure `.env`

You should now have at least the SiliconFlow API key. Open `${SEMBR_DIR}/.env` and write the values. Use `sed` or a small Python one-liner — do **not** open an interactive editor on the user's behalf.

```bash
cd "${SEMBR_DIR}"

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

### Access mode — who should be able to reach this sembr

This decision must be made **before** Phase 5 starts the stack, because it controls the docker-compose port binding (`SEMBR_BIND_ADDR`). The shipped default is `0.0.0.0` — i.e. **anyone on the same LAN as this machine can already hit `http://<this-host-ip>:8000` once the stack is up**. Most home / LAN installs are fine with that; some setups want it locked down. Surface the choice explicitly.

**Tell user:**

> "Before I bring sembr up, choose how it should be reachable:
>
> | Option | Who can reach the dashboard | Pick this when |
> | --- | --- | --- |
> | **A. localhost-only** | Only programs running on this host | You're the only user, or you'll SSH-tunnel in, or sembr lives on a server with no UI users |
> | **B. LAN** (the current default) | You + anyone on your Wi-Fi / office LAN | Home server, lab box, workstation on a network you trust |
> | **C. Public internet** | Anyone, via your domain through a reverse proxy with TLS | You're on a VPS / cloud VM with a public IP and a domain |
>
> ⚠️ **Heads-up for option C:** Cloudflare Tunnel (and trycloudflare) terminate idle SSE connections after ~100 s. The dashboard **Logs** tab streams live logs via SSE, so it will silently stop updating through Cloudflare. Caddy and nginx do not have this limit — pick those if real-time logs matter to you. Everything else (intent/feed CRUD, fire, settings) works fine on all options."
>
> Separately — once I'm done installing, do you want to keep driving sembr through me (or another AI agent like Cursor, a custom script), or only through the web dashboard?"

**Ask user (single-select on binding):** "Pick A / B / C. Default A if unsure — you can always reopen later."
**Ask user (yes/no on agent):** "And after install, will you want agent-driven access — i.e. keep working with me (or another agent) on sembr, or just use the web dashboard?"

#### Apply the binding choice

For each branch, edit `.env` so Phase 5 brings the stack up with the right binding from the start.

**A — localhost-only:**

```bash
cd "${SEMBR_DIR}"
# Pin to loopback. SEMBR_BIND_ADDR is read by docker-compose.yml.
if grep -q '^SEMBR_BIND_ADDR=' .env; then
  sed -i.bak 's|^SEMBR_BIND_ADDR=.*|SEMBR_BIND_ADDR=127.0.0.1|' .env
else
  echo 'SEMBR_BIND_ADDR=127.0.0.1' >> .env
fi
rm -f .env.bak
```

Tell user: "Dashboard will be reachable only at `http://localhost:${PORT}` from this machine. To open it later, delete the `SEMBR_BIND_ADDR` line in `.env` and run `docker compose up -d --force-recreate api`."

**B — LAN (default):**

No `.env` change needed — `SEMBR_BIND_ADDR` left unset, docker-compose default `0.0.0.0` wins. You'll print the LAN URL in Phase 6's final summary.

**Strongly recommend** setting `DASHBOARD_TOKEN` even on LAN — anyone on the same Wi-Fi (housemates, guests, untrusted IoT devices) can otherwise post to `/api/*` and run up your SiliconFlow bill. If you didn't already generate one above, do it now:

```bash
cd "${SEMBR_DIR}"
if ! grep -qE '^DASHBOARD_TOKEN=.+' .env; then
  TOKEN=$(openssl rand -hex 16)
  sed -i.bak "s|^DASHBOARD_TOKEN=.*|DASHBOARD_TOKEN=${TOKEN}|" .env
  rm -f .env.bak
  echo "DASHBOARD_TOKEN=${TOKEN}"   # show to user
fi
```

**C — public internet:**

Apply the loopback binding (same as branch A) so the API is **not** reachable on the public interface directly. The reverse proxy will forward to `127.0.0.1:8000`.

```bash
cd "${SEMBR_DIR}"
if grep -q '^SEMBR_BIND_ADDR=' .env; then
  sed -i.bak 's|^SEMBR_BIND_ADDR=.*|SEMBR_BIND_ADDR=127.0.0.1|' .env
else
  echo 'SEMBR_BIND_ADDR=127.0.0.1' >> .env
fi
rm -f .env.bak
```

Also force-generate `DASHBOARD_TOKEN` if empty — public deployments **must not** run unauthenticated:

```bash
if ! grep -qE '^DASHBOARD_TOKEN=.+' .env; then
  TOKEN=$(openssl rand -hex 32)   # 64-char for public exposure
  sed -i.bak "s|^DASHBOARD_TOKEN=.*|DASHBOARD_TOKEN=${TOKEN}|" .env
  rm -f .env.bak
  echo "DASHBOARD_TOKEN=${TOKEN}"
fi
```

**Tell user:**

> "Locked sembr to loopback and minted a 64-char `DASHBOARD_TOKEN`. The dashboard is NOT yet reachable on the internet — that's intentional. The next stretch (DNS, reverse proxy + TLS, compose-level port lockdown, firewall) needs your input on the domain and cloud provider, so I'm not going to guess. I'll switch to **`PUBLIC_INSTALL.md`** to walk through it; when those steps are done we come back here and finish Phase 5 → 6 normally."

Now **branch into `PUBLIC_INSTALL.md` (Steps 1–5)** and complete it. When you return, `${SEMBR_DIR}/.env` is still set up for loopback + token, but the host now also has DNS pointing here, a reverse proxy listening on 443 (or a Cloudflare Tunnel), compose patched to bind the side services on loopback, ufw allowing only 22 / 80 / 443, and a recorded decision on the `docker.sock` mount. After `PUBLIC_INSTALL.md` returns, continue with Phase 5 below — Phase 5 includes a branch-C-specific external verification block.

#### Apply the agent choice

If user said **yes** to agent-driven access:

**Agent — mint a token if none exists:**

```bash
cd "${SEMBR_DIR}"
# Agents call /api/* with X-Dashboard-Token header. An empty token means
# anyone with network reach can drive the API, so we mint one if absent.
if ! grep -qE '^DASHBOARD_TOKEN=.+' .env; then
  TOKEN=$(openssl rand -hex 16)
  sed -i.bak "s|^DASHBOARD_TOKEN=.*|DASHBOARD_TOKEN=${TOKEN}|" .env
  rm -f .env.bak
  echo "DASHBOARD_TOKEN=${TOKEN}"
fi
```

**Agent — install the skill bundle for yourself (if your platform supports it):**

The repo ships a skill bundle at `${SEMBR_DIR}/agent/sembr/` — `SKILL.md` plus `references/{endpoints,schemas,recipes,errors}.md` — that teaches an AI agent to drive sembr's HTTP API.

**Only one platform-specific path is verified to actually load skills from a known directory:**

- **Claude Code** → `~/.claude/skills/sembr/`

If you are Claude Code:

```bash
mkdir -p ~/.claude/skills
cp -r "${SEMBR_DIR}/agent/sembr" ~/.claude/skills/sembr
echo "installed to ~/.claude/skills/sembr/"
```

**If you are any other platform** (Cursor, OpenClaw, Hermes, a custom agent, or anything else): don't guess at a path. Different agents load context differently — some have skills folders, some don't, the conventions vary. Tell the user instead:

> "The sembr agent skill bundle is at `${SEMBR_DIR}/agent/sembr/` (`SKILL.md` plus `references/`). I'm not certain where your agent platform loads skill bundles from, so I won't copy blindly. Either (a) consult your platform's docs and copy the folder there yourself, or (b) hand `${SEMBR_DIR}/agent/sembr/SKILL.md` directly to whichever agent will operate sembr."

If user said **no** to agent-driven access, skip both — they can find the bundle at `${SEMBR_DIR}/agent/sembr/` whenever they want.

### Ask user — anything else they want

**Ask user:** "Anything else to configure right now? (Common: change `LLM_MODEL` to a non-SiliconFlow endpoint, change Qdrant or SQLite paths, set `SEMBR_HOST_PORT` if 8000 is taken). If unsure, say 'no' and we'll move on."

If 'no' — proceed to Phase 5.

---

## Phase 5 — Bring up and verify

### Wait for the background work

```bash
# Wait for pull + build (started in Phase 3). If either failed, surface the log.
# PIDs were persisted to /tmp by Phase 3 so this works across shell reconnects.
wait $(cat /tmp/sembr-pull.pid)  || ( echo "pull failed:" && tail -30 /tmp/sembr-pull.log && exit 1 )
wait $(cat /tmp/sembr-build.pid) || ( echo "build failed:" && tail -30 /tmp/sembr-build.log && exit 1 )
```

**On failure during pull/build:** read the tail of the log, surface the salient error to the user, and ask whether to retry or abort. Common: transient network → retry usually works; `apt-get` mirror down inside Dockerfile → retry in 5 min.

### Start the stack

```bash
cd "${SEMBR_DIR}"
docker compose up -d
```

### Poll `/health` until ready

```bash
cd "${SEMBR_DIR}"   # ensure we're in the install dir for .env lookup + docker compose
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

### Branch C only — external verification

Skip this block for branches A and B.

If the user picked **trycloudflare** in `PUBLIC_INSTALL.md` Step 1.2, start the tunnel now (the stack is up and `/health` is green).

The tunnel process must survive the agent's SSH session ending — a bare `&` dies on SIGHUP. Use `nohup` + `disown` (no systemd needed, no extra dependency):

```bash
# Start the tunnel detached from this shell so it survives SSH disconnect.
nohup cloudflared tunnel --url "http://127.0.0.1:${PORT}" \
  > /tmp/sembr-cf-tunnel.log 2>&1 &
CLOUDFLARED_PID=$!
disown
echo "${CLOUDFLARED_PID}" > /tmp/sembr-cf-tunnel.pid

# cloudflared takes a few seconds (DNS, connect) before it prints the URL.
# Poll the log for up to 30 s. -oE is portable (busybox / Alpine); avoid -oP.
TUNNEL_URL=""
for i in $(seq 1 30); do
  TUNNEL_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \
    /tmp/sembr-cf-tunnel.log | head -1)
  [ -n "${TUNNEL_URL}" ] && break
  sleep 1
done

if [ -z "${TUNNEL_URL}" ]; then
  echo "ERROR: tunnel URL did not appear within 30 s. Log tail:" >&2
  tail -30 /tmp/sembr-cf-tunnel.log >&2
  exit 1
fi
echo "tunnel URL: ${TUNNEL_URL}"

# Sanity: the tunnel must proxy /health.
curl -fsS -m 10 "${TUNNEL_URL}/health" && echo " ✓ tunnel working"
```

**Pass condition:** `/health` returns 200 through the tunnel URL. No port probes needed — Cloudflare's edge only exposes 443, and the tunnel is an outbound connection from this VM.

**Tell user:**

> "sembr is reachable at **${TUNNEL_URL}**. The tunnel process (pid `${CLOUDFLARED_PID}`, logged to `/tmp/sembr-cf-tunnel.log`) is detached from my session, so it survives me disconnecting — but it does **not** restart on reboot. If the VM reboots, the URL changes. To make it permanent, re-run with a registered domain and pick option C (Cloudflare Tunnel proper) or B (Caddy / nginx)."

If the user picked a **real domain** (Caddy / nginx / Cloudflare Tunnel proper), run the full verification below.

The reverse proxy and ufw were set up in `PUBLIC_INSTALL.md` before the stack came up. Now that sembr is running on loopback, confirm that **(1)** the proxy forwards `${DOMAIN}` traffic to it, **(2)** the gated paths actually 401 without a token, and **(3)** the non-public ports stay unreachable.

```bash
DOMAIN="${DOMAIN:-<from PUBLIC_INSTALL.md Step 1.2>}"
TOKEN=$(grep -E '^DASHBOARD_TOKEN=' "${SEMBR_DIR}/.env" | cut -d= -f2)

echo "→ https://${DOMAIN}/health  (expect 200, no auth)"
curl -sI -m 10 "https://${DOMAIN}/health" | head -1

echo "→ https://${DOMAIN}/intents  (no token: expect 401)"
curl -sI -m 10 "https://${DOMAIN}/intents" | head -1

echo "→ https://${DOMAIN}/intents  (wrong token: expect 401)"
curl -sI -m 10 -H "X-Dashboard-Token: wrong" "https://${DOMAIN}/intents" | head -1

echo "→ https://${DOMAIN}/intents  (correct token: expect 200)"
curl -sI -m 10 -H "X-Dashboard-Token: ${TOKEN}" "https://${DOMAIN}/intents" | head -1

# Probe the ports that MUST NOT be open from outside. curl is everywhere; nmap is optional.
# Each line should print "✓ closed" (timeout or refused). If you see "✗ OPEN" anywhere, stop
# and re-check PUBLIC_INSTALL.md Step 2 (compose port lockdown).
echo "→ external port probes via ${DOMAIN}"
for port in 8000 6333 6334 1200; do
  if curl -m 4 -sf "http://${DOMAIN}:${port}/" -o /dev/null 2>&1; then
    echo "  ✗ port ${port} ANSWERED — exposure leak; fix before letting anyone hit ${DOMAIN}"
  else
    echo "  ✓ port ${port} closed"
  fi
done
```

**Pass conditions (all must hold):**
- **trycloudflare**: `/health` through the tunnel URL → 200.
- **Domain-based**: `/health` → 200, `/intents` without / wrong token → 401, `/intents` with correct token → 200, all four non-public ports (8000 / 6333 / 6334 / 1200) → closed.

**On failure — any of the must-be-closed ports answered:** sembr is exposed beyond the reverse proxy. Most likely causes: `PUBLIC_INSTALL.md` Step 2's sed didn't match (custom compose indent), or `SEMBR_BIND_ADDR` isn't `127.0.0.1`. Inspect `docker-compose.yml` and `.env`, fix, then `docker compose up -d --force-recreate api qdrant rsshub` and re-probe.

(If `nmap` is installed on the user's laptop, an off-VM `nmap -p 80,443,8000,6333,6334,1200,22 ${DOMAIN}` from a different network is the gold-standard check — curl from the same VM goes out and comes back, which is close to but not identical to a stranger's request.)

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
TOKEN=$(grep -E '^DASHBOARD_TOKEN=' "${SEMBR_DIR}/.env" | cut -d= -f2)

# Build the body with `jq -n --arg` so quotes / backslashes / newlines in
# INTENT_TEXT can't break out of the JSON string (natural-language intents
# occasionally contain double quotes — don't string-interpolate into JSON).
BODY=$(jq -n \
  --arg name "first" \
  --arg text "${INTENT_TEXT}" \
  --arg tz "${TZ}" \
  --arg recipient "${RECIPIENT}" \
  '{
     name: $name,
     text: $text,
     timezone: $tz,
     schedule: {mode: "cron", preset: "daily", hour: 9, minute: 0},
     channels: [{type: "email", to: [$recipient]}]
   }')

curl -X POST "http://localhost:${PORT}/intents" \
  -H "Content-Type: application/json" \
  ${TOKEN:+-H "X-Dashboard-Token: ${TOKEN}"} \
  -d "${BODY}"
```

Expected response: HTTP 201 with the created intent's JSON, including its assigned `id`.

If the user didn't configure SMTP, drop `channels` from the `jq` template (or pass `[]`) so the intent stores but won't try to email — the user can edit channels later via the dashboard.

If `jq` isn't installed (`command -v jq || sudo apt-get install -y jq`) — just install it; it's a tiny dependency and you'll want it for any future API plumbing.

---

## Done — final summary to user

Before printing the summary, detect this machine's primary LAN IP so users who picked branch B get a clickable URL (not just `localhost`):

```bash
# Cross-platform best-effort. Empty string is fine — we'll just hide the LAN line.
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')                                # Linux (most)
[ -z "$LAN_IP" ] && LAN_IP=$(ipconfig getifaddr en0 2>/dev/null)                    # macOS Wi-Fi / primary
[ -z "$LAN_IP" ] && LAN_IP=$(ipconfig getifaddr en1 2>/dev/null)                    # macOS secondary
[ -z "$LAN_IP" ] && LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
# Sanity: keep only RFC1918 / link-local (don't accidentally print a public IP)
case "${LAN_IP}" in
  10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|169.254.*) : ;;
  *) LAN_IP="" ;;
esac
echo "LAN_IP=${LAN_IP:-<none detected>}"
```

Then **tell user** — relay only the bullets that match their Phase 4 access-mode choice. Drop the others; don't recite options that don't apply.

> "sembr is running.
>
> **How to reach it:**

**Branch A (localhost-only):**
> > - Dashboard: http://localhost:${PORT}/dashboard {if DASHBOARD_TOKEN set: '— log in with the token I generated earlier'}
> > - API: http://localhost:${PORT}
> > - Reachable only from this machine. To open up later: clear `SEMBR_BIND_ADDR` in `.env` and `docker compose up -d --force-recreate api`.

**Branch B (LAN):**
> > - From this machine: http://localhost:${PORT}/dashboard
> > - From any device on your LAN: **http://${LAN_IP}:${PORT}/dashboard** ← share this with the other devices on your Wi-Fi
> > - {if DASHBOARD_TOKEN set: 'Log in with the token I generated earlier — required for both URLs.'}
> > - {if DASHBOARD_TOKEN empty: '⚠️ No token set — anyone on the same Wi-Fi (including guests / IoT devices) can drive your sembr. Strongly consider setting `DASHBOARD_TOKEN` in .env and `docker compose restart api`.'}

**Branch C (public, after they finish the manual reverse-proxy work):**
> > - From this machine for testing: http://localhost:${PORT}/dashboard (loopback-only binding — won't work from elsewhere yet)
> > - Public URL: https://${DOMAIN}/dashboard ← live now that `PUBLIC_INSTALL.md` Steps 1–5 are done and the stack is up
> > - The branch-C external verification (below) confirms 8000 / 6333 / 6334 / 1200 are NOT reachable from outside.

> **Common to all three:**
> - Health probe: http://localhost:${PORT}/health (this stays loopback-friendly even in branch C)
> - Data: `${SEMBR_DIR}/data/` (SQLite + Qdrant storage — back this up)
> - Logs: `docker compose logs -f api` from `${SEMBR_DIR}`
> - Stop / start: `docker compose down` / `docker compose up -d` from `${SEMBR_DIR}`
> - **Drive from an AI agent**: see `${SEMBR_DIR}/agent/sembr/` — Agent Skills bundle with curl + Python recipes for every endpoint
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
| Filesystem warnings in logs | `./data` on a network share (NFS / SMB / virtio-9p) | Move `${SEMBR_DIR}` to a local-disk path; SQLite WAL is unsafe on network shares |

---

## What NOT to do (agent guardrails)

- **Don't** modify code under `sembr/` or rewrite `docker-compose.yml`. This is the user's deployment, not a dev install. Configuration belongs in `.env` and runtime overrides — never in committed code.
- **Don't** install Python packages, run `uv sync`, or run `pytest` on the host. Everything runs inside Docker.
- **Don't** run `git pull` after the initial clone. Leave the user at the launch tag.
- **Don't** publish or expose the dashboard to the public internet without setting `DASHBOARD_TOKEN` and running through `PUBLIC_INSTALL.md`. The dashboard editor is effectively root on the host via the Docker socket mount (which `PUBLIC_INSTALL.md` Step 5 will offer to disable).
- **Don't** commit the user's `.env` to any repo. It contains their API keys.
- **Don't** delete `${SEMBR_DIR}/data/` to "clean up" — that's where the SQLite DB and Qdrant vectors live. Confirm before any destructive operation.
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
| `DASHBOARD_TOKEN` | conditional | empty | **Must set** if exposing beyond localhost (LAN or public) |
| `SEMBR_BIND_ADDR` | optional | `0.0.0.0` | Set to `127.0.0.1` for localhost-only or for reverse-proxy / agent-only setups |
| `SEMBR_HOST_PORT` | optional | `8000` | Override if 8000 is in use |

---

## Versioning

This guide tracks sembr `main`. For a specific version, prefix the URL with the tag: `https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/agent/INSTALL.md`.
