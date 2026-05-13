# sembr — Agent-Driven Public-Exposure Guide

> **This document is written for an AI agent** (OpenClaw, Hermes, Claude Code or similar) operating on the user's **public-facing VM**. You should reach this doc as the continuation of [`INSTALL.md`](INSTALL.md) when the user picked **branch C (public internet)** in Phase 4's access-mode question.
>
> A human can follow this guide too, but the structure — phases, parallel-safe steps, "Tell user" / "Ask user" labels — is optimised for autonomous agent execution.

## State you are arriving in

If you came through `INSTALL.md` Phases 1–5 with branch C, the following is already true on this host. Don't redo it.

- `${SEMBR_DIR}/.env` exists with:
  - `EMBEDDER_API_KEY=` + `LLM_API_KEY=` filled in
  - `SEMBR_BIND_ADDR=127.0.0.1` (api published on loopback only)
  - `DASHBOARD_TOKEN=` set to a 64-char hex value
- `docker compose up -d` is running. `curl http://localhost:${SEMBR_HOST_PORT:-8000}/health` returns `{"status":"ok"}`
- You hold `${SEMBR_DIR}` and `${PORT}` as shell variables from the previous guide. If you opened a fresh shell, re-export them now (`SEMBR_DIR=...`, `PORT=$(grep -E '^SEMBR_HOST_PORT=' "${SEMBR_DIR}/.env" | cut -d= -f2)`, `PORT=${PORT:-8000}`).

If any of the above is **not** true, stop and finish `INSTALL.md` first. Public exposure on top of a broken or unauthenticated stack is worse than no exposure at all.

## How to use this document

Same conventions as `INSTALL.md`:

| Block | Meaning |
| --- | --- |
| **Agent** | Shell commands you run yourself and inspect the output of |
| **Tell user** | Information you surface to the user |
| **Ask user** | A blocking question; wait for the user's reply before continuing |
| **Pass / On failure** | The condition that ends the phase, and what to do if it doesn't hold |

**Speak the user's language.** Translate Tell-user / Ask-user blocks into whatever language the user has been chatting with you in.

**Consent before privileged action.** Every `sudo`, every `ufw enable`, every `/etc/ssh/sshd_config` edit must be surfaced verbatim and approved before you run it. Public-VM tools have permanent consequences — a wrong `ufw default deny incoming` can lock you out of SSH.

**Order matters.** Phases 7 → 11 must run in sequence. In particular, do **not** enable `ufw` (Phase 10) before the reverse proxy is listening (Phase 9), and do **not** restrict SSH (Phase 11) before you've validated you can still reach the box.

---

## Phase 7 — Prerequisites & DNS

### 7.1 Confirm this really is a public VM

**Agent:**

```bash
# Public-facing IPv4 of the box (asks ifconfig.me, doesn't expose anything new)
PUBLIC_IP=$(curl -sf -m 5 https://ifconfig.me 2>/dev/null || curl -sf -m 5 https://api.ipify.org 2>/dev/null || echo "")
echo "public IP: ${PUBLIC_IP:-<could not detect>}"

# Anything currently listening on 80 / 443?
sudo ss -ltnp 2>/dev/null | awk '$4 ~ /:(80|443)$/' || true
```

**Pass conditions:**
- `${PUBLIC_IP}` is a public address (not RFC1918, not `127.0.0.1`). If empty, ask the user to paste the VM's public IP and store it.
- Ports 80 and 443 are not already in use by another service. If they are, ask the user what's running there before continuing.

**On failure — RFC1918 address detected:** This isn't a public VM. The user probably meant branch B (LAN) — surface this and ask whether they want to back out to LAN mode (clear `SEMBR_BIND_ADDR` from `.env` and `docker compose up -d --force-recreate api`).

### 7.2 Domain name and DNS

**Ask user:**

> "What domain (or subdomain) should sembr live at? Example: `sembr.your-domain.com`. I'll need this for the TLS certificate. If you don't have a domain yet, you can register one in 10 minutes at Cloudflare / Namecheap / Porkbun. Paste the domain when ready."

Store as `${DOMAIN}`.

**Agent — verify DNS A record points here:**

```bash
DOMAIN="<the user's answer>"

# Resolve via the public DNS (system resolver might cache stale records)
RESOLVED=$(dig +short "${DOMAIN}" @1.1.1.1 A | tail -1)
echo "domain ${DOMAIN} → ${RESOLVED:-<NXDOMAIN>}"
echo "this VM public IP → ${PUBLIC_IP}"

if [ -z "${RESOLVED}" ]; then
  echo "  ✗ no A record yet"
elif [ "${RESOLVED}" = "${PUBLIC_IP}" ]; then
  echo "  ✓ DNS matches"
else
  echo "  ✗ DNS points at ${RESOLVED}, expected ${PUBLIC_IP}"
fi
```

**Pass condition:** `dig` returns this VM's public IP.

**On failure — no record / wrong record:**

**Tell user:**

> "DNS isn't pointed at this VM yet. In your domain provider's DNS panel, create:
> - **Type:** A
> - **Name:** `<subdomain>` (e.g. `sembr`, or `@` if `${DOMAIN}` is the apex)
> - **Value:** `${PUBLIC_IP}`
> - **TTL:** 5 min or default
>
> If your domain is on Cloudflare, set the **proxy** toggle to **DNS only** (grey cloud) for now — orange-cloud changes the source IP we'd see and breaks Let's Encrypt HTTP-01. You can flip it on later if you prefer.
>
> Propagation usually completes in a minute; up to 30 in the worst case. Tell me when you've added the record and I'll re-check."

Loop on `dig +short "${DOMAIN}" @1.1.1.1` every 30s, max 30 minutes, until the answer matches `${PUBLIC_IP}`. If it doesn't, surface the diff and ask the user to double-check their DNS panel.

### 7.3 Pick your TLS strategy

**Tell user:**

> "Three TLS / reverse-proxy options. Pick one:
>
> | Option | Best for | Trade-off |
> | --- | --- | --- |
> | **A. Caddy** (recommended) | First-time deployments, no certbot machinery | Caddy handles Let's Encrypt automatically; one ~15-line `Caddyfile` |
> | **B. nginx + certbot** | You already run nginx, or want to integrate with an existing nginx config | More steps, more knobs; well-trodden path |
> | **C. Cloudflare Tunnel** | You want **no inbound port open** on the VM (deny-all firewall + SSH only); fine with Cloudflare being in front | Requires a Cloudflare account; CF terminates TLS, not you |
>
> Default: **A. Caddy.** Pick A unless you have a specific reason."

**Ask user:** "A / B / C?" — store as `${TLS_OPTION}`.

---

## Phase 8 — Lock down the side services

`docker-compose.yml` publishes the API behind `${SEMBR_BIND_ADDR}`, but **`qdrant` (6333 / 6334) and `rsshub` (1200) are hardcoded to `0.0.0.0`**. On a LAN box that's fine; on a public VM, leaving Qdrant's HTTP API or RSSHub's URL-fetcher open to the world is a serious problem. Qdrant has no auth by default; RSSHub is a willing SSRF gadget.

You have two ways to close this. Pick by feel of the deployment.

### 8.A (preferred) — edit `docker-compose.yml` for these two services

This is the one place where `INSTALL.md`'s "don't touch compose" rule deliberately bends. Show the diff to the user before applying.

**Tell user:**

> "I need to bind Qdrant and RSSHub to loopback too — they currently publish on `0.0.0.0` regardless of `SEMBR_BIND_ADDR`. I'll patch four lines in `docker-compose.yml`. Diff:
>
> ```diff
>    rsshub:
>      ports:
> -     - "1200:1200"
> +     - "127.0.0.1:1200:1200"
>
>    qdrant:
>      ports:
> -     - "6333:6333"
> -     - "6334:6334"
> +     - "127.0.0.1:6333:6333"
> +     - "127.0.0.1:6334:6334"
> ```
>
> Apply?"

**Ask user:** "Apply? (yes / no)" — if no, fall back to Phase 8.B.

**Agent:**

```bash
cd "${SEMBR_DIR}"
cp docker-compose.yml docker-compose.yml.bak

# Idempotent: only rewrites lines that don't already have a bind-addr prefix.
sed -i 's|^\(\s*-\s\+\)"1200:1200"|\1"127.0.0.1:1200:1200"|; \
        s|^\(\s*-\s\+\)"6333:6333"|\1"127.0.0.1:6333:6333"|; \
        s|^\(\s*-\s\+\)"6334:6334"|\1"127.0.0.1:6334:6334"|' docker-compose.yml

diff docker-compose.yml.bak docker-compose.yml || true

docker compose up -d --force-recreate qdrant rsshub
```

**Pass condition:** `ss -ltnp` shows ports 1200 / 6333 / 6334 bound to `127.0.0.1`, not `0.0.0.0`.

```bash
sudo ss -ltnp | awk '$4 ~ /:(1200|6333|6334)$/'
# All three must show 127.0.0.1:PORT, not 0.0.0.0:PORT or *:PORT.
```

### 8.B (fallback) — block at the firewall instead

If the user said no to editing compose, defer the lockdown to ufw in Phase 10 (`ufw default deny incoming` will close 1200 / 6333 / 6334 even if they're published on 0.0.0.0, since the firewall sits in front of Docker's iptables rules **only when `ufw-docker` is configured**). Make a note on the install report — this branch is more fragile because Docker often punches its own holes through ufw.

---

## Phase 9 — Reverse proxy + TLS

### 9.A — Caddy (recommended)

**Agent — install Caddy:**

**Ask user:** "Install Caddy via the official apt repo? I'll run the snippets at https://caddyserver.com/docs/install. Proceed?"

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update
sudo apt-get install -y caddy
caddy version
```

**Agent — write the Caddyfile:**

```bash
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
${DOMAIN} {
    encode gzip zstd

    # Belt-and-braces: only allow the HTTP methods sembr actually uses.
    @methods {
        not method GET POST PUT PATCH DELETE OPTIONS HEAD
    }
    respond @methods 405

    # SSE needs flush_interval -1 so events arrive immediately.
    reverse_proxy 127.0.0.1:${PORT} {
        flush_interval -1
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-For {remote_host}
    }

    # Headers Caddy adds on top of whatever sembr returns.
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        Referrer-Policy strict-origin-when-cross-origin
        -Server
    }
}
EOF

sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

**Pass condition:** `journalctl -u caddy --since '2 min ago' --no-pager` shows `certificate obtained successfully` for `${DOMAIN}`. Caddy fetches a Let's Encrypt cert on first request (or within ~60 s of reload).

**On failure — "no such host" / DNS resolution error:** DNS hasn't propagated yet. Go back to Phase 7.2 and wait.

**On failure — "challenge failed" / port 80 unreachable:** the cloud provider's network firewall (security group, not ufw) is blocking 80. Tell the user to open 80 and 443 inbound in their cloud console (AWS Security Group, DigitalOcean Firewall, etc.).

### 9.B — nginx + certbot

**Ask user:** "Install nginx and certbot? I'll add the official certbot repo if it isn't already there. Proceed?"

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

**Agent — write the site config and enable it:**

```bash
sudo tee /etc/nginx/sites-available/sembr >/dev/null <<EOF
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    # certbot will fill these in on first run.
    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    client_max_body_size 5m;

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # SSE / streaming
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/sembr /etc/nginx/sites-enabled/sembr
# Temporarily comment out the ssl_* lines so nginx -t passes before certbot runs.
sudo sed -i.bak '/ssl_certificate/s/^/# /' /etc/nginx/sites-available/sembr
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "<user-email-here>"

# certbot un-comments the ssl_certificate lines automatically; verify and reload.
sudo nginx -t && sudo systemctl reload nginx
```

**Ask user** before running certbot: "I need an email address for Let's Encrypt expiration notices. Use `${SMTP_FROM:-<the user's email>}` or paste a different one." Substitute into the `-m` flag above.

**Pass condition:** `curl -Is https://${DOMAIN}/health | head -1` → `HTTP/2 200`.

### 9.C — Cloudflare Tunnel

**Ask user:** "Cloudflare Tunnel needs (1) a Cloudflare account with `${DOMAIN}`'s zone already added, and (2) you to authenticate `cloudflared` interactively in a browser. I can install `cloudflared` and set up the tunnel skeleton, but you'll need to run the `cloudflared tunnel login` step yourself. Proceed?"

```bash
# Install cloudflared (Debian/Ubuntu)
curl -L https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared focal main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
sudo apt-get update
sudo apt-get install -y cloudflared
cloudflared --version
```

**Tell user:**

> "Now run these on this VM yourself — they need a browser login:
>
> ```bash
> cloudflared tunnel login              # opens a URL — paste it into your browser to authenticate
> cloudflared tunnel create sembr        # gives you a UUID and writes ~/.cloudflared/<UUID>.json
> ```
>
> Tell me the tunnel UUID when done."

**Agent — once UUID is known:**

```bash
TUNNEL_UUID="<from the user>"
mkdir -p ~/.cloudflared

cat > ~/.cloudflared/config.yml <<EOF
tunnel: ${TUNNEL_UUID}
credentials-file: /home/$(whoami)/.cloudflared/${TUNNEL_UUID}.json
ingress:
  - hostname: ${DOMAIN}
    service: http://127.0.0.1:${PORT}
    originRequest:
      noTLSVerify: true
      disableChunkedEncoding: false
  - service: http_status:404
EOF

cloudflared tunnel route dns "${TUNNEL_UUID}" "${DOMAIN}"
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Cloudflare DNS gets a CNAME automatically; no manual A-record needed (Phase 7.2 was optional for this branch — but the user already added one, so don't worry about cleanup).

**Pass condition:** `curl -Is https://${DOMAIN}/health` → `HTTP/2 200`.

---

## Phase 10 — Firewall

Even with the reverse proxy in front, lock the rest of the surface down. `ufw` is the default tool on Debian / Ubuntu; if the user is on a different distro or already runs `firewalld` / `iptables` rules, ask before clobbering.

**Ask user:**

> "Set up `ufw` to allow only 22 (SSH), 80, 443 (reverse-proxy traffic), and drop everything else inbound? This will also implicitly drop direct hits to 8000 / 6333 / 6334 / 1200 from outside.
>
> ⚠️ Important: once I enable ufw, the SSH rule must be in place first — otherwise this very session can drop. If you're in any doubt about which IP you SSH from, allow 22 from anywhere (`sudo ufw allow 22/tcp`) and tighten later. Proceed?"

**Agent:**

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing

sudo ufw allow 22/tcp comment 'ssh'

# Caddy/nginx branches need 80 (HTTP-01 + redirect) and 443
if [ "${TLS_OPTION}" != "C" ]; then
  sudo ufw allow 80/tcp  comment 'http-01 + redirect'
  sudo ufw allow 443/tcp comment 'sembr https'
fi
# Cloudflare Tunnel branch: no inbound port at all besides SSH

sudo ufw --force enable
sudo ufw status verbose
```

**Pass condition:** `ufw status` shows the rules, **and** an SSH session check still works (see verification in Phase 11.3).

**On failure — you lost your SSH session:** the user has to either (a) use the cloud provider's web console / serial console to log in and run `ufw disable`, or (b) wait for `ufw` to be off after a reboot (it isn't — it persists). Surface this risk *before* enabling.

---

## Phase 11 — SSH hardening

This is the easiest thing to get wrong at the worst time. Do it carefully.

### 11.1 Verify the user can already log in with a key

Before changing anything in `sshd_config`, confirm the user has a working key-based login. Otherwise disabling password auth will lock them out.

**Ask user:** "From your laptop, can you `ssh user@${DOMAIN}` *without typing a password* — i.e. via SSH key? If yes, we're safe to disable password auth. If no, stop here and add your public key to `~/.ssh/authorized_keys` on this VM first."

**Agent — quick sanity:**

```bash
ls -la ~/.ssh/authorized_keys 2>/dev/null && wc -l ~/.ssh/authorized_keys
```

Should be at least one key. If zero, **stop**. Tell the user how to add one (`ssh-copy-id` from their laptop, or paste the public key into `~/.ssh/authorized_keys`).

### 11.2 Apply hardening (only after 11.1 passes)

**Ask user:** "I'll edit `/etc/ssh/sshd_config` to: disable root login, disable password auth, disable challenge-response auth. Diff shown below. Proceed?"

```diff
- PermitRootLogin yes      (or unset)
+ PermitRootLogin no
- PasswordAuthentication yes
+ PasswordAuthentication no
+ ChallengeResponseAuthentication no
+ KbdInteractiveAuthentication no
+ UsePAM yes
```

**Agent:**

```bash
sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak

# Replace if present, else append. Idempotent.
sudo python3 - <<'PY'
from pathlib import Path
p = Path("/etc/ssh/sshd_config")
text = p.read_text().splitlines()
desired = {
    "PermitRootLogin": "no",
    "PasswordAuthentication": "no",
    "ChallengeResponseAuthentication": "no",
    "KbdInteractiveAuthentication": "no",
    "UsePAM": "yes",
}
keys_seen = set()
out = []
for line in text:
    stripped = line.strip()
    matched = False
    for k, v in desired.items():
        if stripped.startswith(k + " ") or stripped.startswith("#" + k + " "):
            out.append(f"{k} {v}")
            keys_seen.add(k)
            matched = True
            break
    if not matched:
        out.append(line)
for k, v in desired.items():
    if k not in keys_seen:
        out.append(f"{k} {v}")
p.write_text("\n".join(out) + "\n")
PY

sudo sshd -t                       # syntax check
sudo systemctl reload ssh
```

### 11.3 Critical: prove SSH still works BEFORE moving on

**Tell user:**

> "Open a **second** terminal on your laptop and `ssh user@${DOMAIN}` *now*, while keeping this current session open. Confirm the new session lands and you get a shell. **Don't close the current session yet** — it's your safety net if the new login fails."

**Ask user:** "Did the new SSH session log in successfully? (yes / no)"

If **no** — restore the backup and reload sshd from the still-open session:

```bash
sudo cp /etc/ssh/sshd_config.bak /etc/ssh/sshd_config
sudo systemctl reload ssh
```

Then investigate (probably the user's key isn't in `authorized_keys` or the key has the wrong perms).

### 11.4 (optional) fail2ban

**Ask user:** "Install `fail2ban` to auto-ban IPs after repeated failed SSH probes? Quick and recommended on public VMs."

```bash
sudo apt-get install -y fail2ban
sudo systemctl enable --now fail2ban
```

Default jail (`sshd`) is enabled out of the box on Debian / Ubuntu — no extra config needed.

---

## Phase 12 — External verification

The whole point of this phase is to confirm that **from outside the VM**, only what should be reachable is reachable. The agent runs on the VM so its `localhost` is fake-public — get the user to test from their laptop.

### 12.1 Self-checks the agent can run

```bash
# These all go out to the public DNS and back in, so they prove the path
DOMAIN="<the domain>"

echo "→ /health (must be 200, no auth)"
curl -sI -m 10 "https://${DOMAIN}/health" | head -1

echo "→ /api/intents without token (must be 401 if you set DASHBOARD_TOKEN)"
curl -sI -m 10 "https://${DOMAIN}/api/intents" | head -1

echo "→ /api/intents with wrong token (must be 401)"
curl -sI -m 10 -H "X-Dashboard-Token: wrong" "https://${DOMAIN}/api/intents" | head -1

echo "→ /api/intents with correct token (must be 200)"
TOKEN=$(grep -E '^DASHBOARD_TOKEN=' "${SEMBR_DIR}/.env" | cut -d= -f2)
curl -sI -m 10 -H "X-Dashboard-Token: ${TOKEN}" "https://${DOMAIN}/api/intents" | head -1
```

**Pass conditions:**
- `/health` → 200
- `/api/intents` unauthenticated → 401
- `/api/intents` wrong token → 401
- `/api/intents` correct token → 200

### 12.2 What the user should check from a different network

**Tell user:**

> "From a machine **not on this VM** — your laptop on a different Wi-Fi works, or just tether through your phone for a moment — run:
>
> ```bash
> curl -i https://${DOMAIN}/health             # should be 200
> nmap -p 80,443,8000,6333,6334,1200,22 ${DOMAIN}
> ```
>
> What `nmap` MUST show:
> - `80/tcp`  → filtered, closed, or 301-redirected (Caddy/nginx branches; CF tunnel may show closed)
> - `443/tcp` → open (Caddy/nginx) or closed (CF tunnel — TLS is on Cloudflare's edge, not this VM)
> - `8000/tcp` → **filtered or closed** ← if this is open, exit and tell me — `SEMBR_BIND_ADDR` isn't loopback
> - `6333/tcp`, `6334/tcp`, `1200/tcp` → **filtered or closed** ← same, Phase 8 wasn't applied
> - `22/tcp` → open or filtered (depending on your `ufw allow 22` rule)
>
> If you see any of the *MUST be closed* ports as open, paste the `nmap` output back to me — that's a real misconfiguration we need to fix before anyone reaches this URL."

**Ask user:** "Run that `nmap`. What did it show?"

If any of the 'must-be-closed' ports show open, walk back and fix — most likely Phase 8 wasn't applied (the qdrant/rsshub edit) or Phase 10's `ufw default deny incoming` didn't actually take effect against Docker's iptables rules (the `ufw` vs Docker tension is a known footgun — install `ufw-docker` if so, or apply Phase 8.A and let it close at the bind level).

---

## Phase 13 — Backups

Public VMs are stolen / wiped / billing-suspended more often than home boxes. Set up an off-site nightly backup before walking away.

**Tell user:**

> "What's on the VM that matters:
>
> | Path | Contents |
> | --- | --- |
> | `${SEMBR_DIR}/data/` | SQLite DB + Qdrant collection + cached articles |
> | `${SEMBR_DIR}/.env` | API keys, SMTP credentials, `DASHBOARD_TOKEN` — encrypt at rest off-host |
> | `${SEMBR_DIR}/prompts/` | Any custom prompt templates you've added |
>
> Where do you want nightly backups to land? Options:
> - Another box you own (rsync over SSH)
> - S3-compatible object storage (`restic` or `rclone`)
> - 'skip for now' — you'll come back to this"

**Ask user:** "Pick a backup target (or 'skip')."

If user picks **rsync to another box**:

```bash
# Replace the destination as the user specifies.
sudo tee /etc/cron.daily/sembr-backup >/dev/null <<EOF
#!/bin/bash
set -e
DEST=user@backup.host:/srv/backups/sembr/\$(hostname)/\$(date +%F)
rsync -az --delete \\
    --exclude='__pycache__' --exclude='.git' \\
    ${SEMBR_DIR}/data ${SEMBR_DIR}/.env ${SEMBR_DIR}/prompts \\
    "\$DEST"
EOF
sudo chmod +x /etc/cron.daily/sembr-backup
```

**Tell user:** "Backup script is in `/etc/cron.daily/sembr-backup` and runs nightly via `cron.daily`. **Encrypt `.env` on the backup target** — it holds your SiliconFlow key, `DASHBOARD_TOKEN`, and SMTP password. `restic` and `age` are common choices."

If user picks **S3 / object storage** or **skip**: tell them what to do (point at `restic` or `rclone` docs) without running anything.

---

## Phase 14 — Monitoring and known limits

### 14.1 External uptime monitor (recommended)

**Tell user:**

> "Set up something to ping `https://${DOMAIN}/health` every few minutes and alert you on failure. Free options:
>
> - **UptimeRobot** — free tier, 5-min interval, email + push notifications
> - **Uptime Kuma** — self-hosted, fancier dashboards, run on a different box (not this one!)
> - **ntfy + cron** on a different box you own: `curl -fs https://${DOMAIN}/health || curl -d 'sembr down' ntfy.sh/your-topic`
>
> Pick whichever fits your habits. `/health` is intentionally unauthenticated so monitors don't need to hold the token."

This is a Tell-user, not an Agent step — the monitor lives elsewhere.

### 14.2 SiliconFlow billing alert

**Tell user:**

> "Check the SiliconFlow billing dashboard **at least weekly**. sembr 1.0 doesn't have per-IP rate-limit middleware yet — if your `DASHBOARD_TOKEN` ever leaks, an authenticated abuser can run up embedding costs fast. Set a daily cost alert in the SiliconFlow console."

### 14.3 Known limits (v1.0)

| Gap in sembr today | Mitigation on this host |
| --- | --- |
| No per-IP rate limit middleware | Use Caddy's `rate_limit` plugin / nginx `limit_req_zone` against `/api/intents` and `/api/external/*` (cap ~60 req/min/IP). Not configured by this guide — surface as a follow-up. |
| No global request-body size limit in app | `client_max_body_size 5m` is already set in the nginx config above; Caddy default is 10 MiB. |
| Container runs as root | Don't bind-mount additional host paths beyond what `docker-compose.yml` ships with. |
| `/var/run/docker.sock` bind-mounted into the API container | Required by the dashboard's "restart RSSHub" button. If the user doesn't need that button, **comment out the mount** in `docker-compose.yml` and `docker compose up -d --force-recreate api`. The feature degrades gracefully; everything else still works. Compromising the API container is otherwise ≈ root on this host. |
| Single shared `DASHBOARD_TOKEN` (no 2FA / SSO) | Rotate if anyone with access leaves; treat like an SSH key. |
| `/health` is unauthenticated by design | Returns liveness only — no leak. |

**Ask user:** "Want me to comment out the docker.sock bind-mount now? You lose the in-dashboard RSSHub-restart button but everything else still works."

If yes:

```bash
cd "${SEMBR_DIR}"
sed -i.bak 's|^\(\s*\)\(- /var/run/docker.sock:/var/run/docker.sock\)|\1# \2|' docker-compose.yml
docker compose up -d --force-recreate api
```

---

## Done — final summary

**Tell user:**

> "sembr is now reachable at **https://${DOMAIN}**.
>
> - Dashboard: https://${DOMAIN}/dashboard — log in with `DASHBOARD_TOKEN` (already stored in `${SEMBR_DIR}/.env`).
> - API root: https://${DOMAIN} — agents send `X-Dashboard-Token: <token>` on every request.
> - Health probe: https://${DOMAIN}/health (no auth required).
>
> Reverse-proxy / TLS: option ${TLS_OPTION}. Cert auto-renews.
>
> **What I did:**
> - Verified DNS for ${DOMAIN} points to ${PUBLIC_IP}.
> - Locked qdrant (6333/6334) and rsshub (1200) to loopback. {if Phase 8.A applied}
> - Set up the reverse proxy and a Let's Encrypt cert. {if A/B}
> - Set up a Cloudflare Tunnel from cloudflared on this VM. {if C}
> - Enabled `ufw` with allows for 22 / 80 / 443 only.
> - Hardened `sshd_config` (no root login, key-only auth).
> - {if installed} fail2ban jail for sshd is active.
> - {if configured} Nightly backup script at `/etc/cron.daily/sembr-backup`.
>
> **What's still on you:**
> - Add an external uptime monitor (Phase 14.1).
> - Set a billing alert on SiliconFlow (Phase 14.2).
> - Rotate `DASHBOARD_TOKEN` if anyone with access ever leaves.
> - Drive the API from an agent? See `${SEMBR_DIR}/agent/sembr/SKILL.md`.
>
> Documentation: https://peakstone-labs.github.io/sembr"

---

## Troubleshooting matrix

Use this if any phase fails or the user reports a problem later.

| Symptom | Most likely cause | Fix |
| --- | --- | --- |
| `dig` returns wrong / no IP for `${DOMAIN}` | DNS not propagated yet, or wrong A record | Wait 5–30 min; re-check the domain provider's DNS panel |
| Caddy logs `tls: trying to obtain certificate but it has no SANs` | DNS hasn't resolved at cert-issuance time | Re-run `sudo systemctl reload caddy` after DNS resolves |
| `Let's Encrypt rate limit exceeded` | Too many failed cert attempts in 1 h | Wait 1 h (Let's Encrypt staging is `--staging`-enabled in certbot for testing) |
| `certbot` says `port 80 not reachable` | Cloud provider's network firewall (separate from ufw) blocks 80 | Open 80 / 443 in the cloud console (Security Group / network ACL) |
| `nmap` from outside shows 8000 open | `SEMBR_BIND_ADDR` not set to `127.0.0.1`, or docker re-published it | `grep SEMBR_BIND_ADDR ${SEMBR_DIR}/.env`; should be `127.0.0.1`; then `docker compose up -d --force-recreate api` |
| `nmap` shows 6333 / 6334 / 1200 open | Phase 8 not applied | Re-do Phase 8.A; `docker compose up -d --force-recreate qdrant rsshub` |
| New SSH login fails after Phase 11 | `authorized_keys` perms wrong or key absent | Use the still-open original session: restore `sshd_config.bak`, reload sshd; fix the key on the laptop side |
| `ufw enable` dropped the agent's SSH session | The `allow 22/tcp` rule wasn't applied before `--force enable` | Use cloud provider's web console / serial console to log in, `sudo ufw disable`, fix |
| Caddy says `cannot bind: address already in use` for 80 | nginx or another service is on 80 | `sudo ss -ltnp \| awk '$4 ~ /:80$/'`; stop the other service |
| sembr dashboard works on loopback but 502 through Caddy/nginx | API container restarted, port closed | `cd ${SEMBR_DIR} && docker compose ps`; restart api; check `/health` on loopback first |
| Cloudflare Tunnel says "connection refused to origin" | `cloudflared` running but `127.0.0.1:${PORT}` not listening | Check `docker compose ps`; `curl -s http://127.0.0.1:${PORT}/health` |

---

## What NOT to do (agent guardrails)

- **Don't** `sudo ufw default deny incoming` without first adding `sudo ufw allow 22/tcp`. Lock yourself out and you're rescuing the user via their cloud provider's web console — embarrassing.
- **Don't** edit `sshd_config` to set `PasswordAuthentication no` until you've confirmed key-based login already works (Phase 11.1).
- **Don't** change `PermitRootLogin` on a box where the user logs in **as root** — they'll be locked out next login. The user must have a non-root account with sudo first.
- **Don't** open additional ports in ufw "because it might be needed". sembr's only public port is 443 (or none, in Cloudflare Tunnel mode).
- **Don't** put `${DASHBOARD_TOKEN}` into a Caddyfile / nginx config / `cloudflared` config in plaintext — it lives in `.env` only. Reverse proxies don't need the token; they just forward.
- **Don't** suggest disabling TLS to "simplify". HTTP-only on a public host leaks `DASHBOARD_TOKEN` on the wire to anyone in the path.
- **Don't** invent a new `DASHBOARD_TOKEN` here — it was minted in `INSTALL.md` Phase 4C and the user has it written down already. Rotating it now stranges their notes.
- **Don't** publish `data/` or `.env` to anywhere world-readable in your backup setup. `.env` holds the SiliconFlow key, SMTP password, and `DASHBOARD_TOKEN` — treat as crown jewels.
- **Don't** uncomment the `/var/run/docker.sock` mount if the user asked you to disable it earlier in this guide.

---

## Reporting security issues

If you find a vulnerability in sembr itself (not in a deployment), report it via GitHub's [Private Vulnerability Reporting](https://github.com/Peakstone-Labs/sembr/security/advisories/new). See [`SECURITY.md`](https://github.com/Peakstone-Labs/sembr/blob/main/SECURITY.md) for details.

---

## Versioning

This guide tracks sembr `main`. For a specific version, prefix the URL with the tag: `https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/agent/public_install.md`.
