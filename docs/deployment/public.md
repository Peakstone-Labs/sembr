# Deploying sembr on a Public-Facing Server

This guide is for users who want to run sembr on a cloud VM (DigitalOcean / Hetzner / Vultr / EC2 / …) and reach the dashboard from anywhere on the internet.

!!! tip "Prefer an AI agent walk through this?"
    [`agent/PUBLIC_INSTALL.md`](https://github.com/Peakstone-Labs/sembr/blob/main/agent/PUBLIC_INSTALL.md) in the repo is an interactive, step-by-step version of the page below for an LLM agent with shell access on the VM. Hand the URL to your agent. The rest of this page is the manual walk-through.

!!! danger "Default config is **not** public-internet safe"
    Out of the box, sembr binds to `0.0.0.0:8000` in plaintext with `DASHBOARD_TOKEN` empty. If you `docker compose up` on a VM with a public IP and open port 8000 in the firewall, **the dashboard is open to the world with no authentication** — and so are Qdrant's 6333/6334 and RSSHub's 1200, which are *also* published on `0.0.0.0` regardless of `SEMBR_BIND_ADDR`. This default is chosen to keep LAN / home-server setups frictionless; the steps below walk you through the changes you must make before exposing sembr publicly.

The recommended path is still a private network (Tailscale / WireGuard / VPN) — see [Getting Started](../getting-started.md). Use this guide only if you actually need a public endpoint.

---

## TL;DR (7-step checklist)

1. Set a strong **`DASHBOARD_TOKEN`** in `.env`.
2. Bind the **API** to loopback (set `SEMBR_BIND_ADDR=127.0.0.1` in `.env`, **or** patch `docker-compose.yml`).
3. **Also bind qdrant (6333/6334) and rsshub (1200) to loopback** in `docker-compose.yml` — these don't honour `SEMBR_BIND_ADDR` and you cannot rely on ufw to close them (Docker bypasses ufw INPUT for published ports).
4. Put sembr behind a **reverse proxy with TLS** (Caddy is the easiest), **or** use a **Cloudflare Tunnel** for no inbound port at all.
5. **Firewall**: allow 443 (skip if Cloudflare Tunnel), allow 22 for SSH, deny everything else.
6. **Verify** with `curl` and `nmap` from outside the VM, probing 8000 / 6333 / 6334 / 1200 are all closed.
7. **VM hygiene** (SSH hardening, fail2ban, backups, unattended-upgrades) — not sembr-specific, but you're now running a public VM. Sections 6, 8 and 9 below cover them.

The rest of this page walks through each step.

---

## 1. Prerequisites

- Cloud VM with a public IPv4 (and IPv6 if your DNS is dual-stacked)
- A domain name pointing at the VM (`A` record for IPv4, `AAAA` for IPv6) — Let's Encrypt and most reverse-proxy auto-TLS flows need a real domain
- Root / sudo access on the VM
- An SSH **key** already in `~/.ssh/authorized_keys` (do **not** rely on a password)
- Docker + Docker Compose installed

## 2. Generate a strong `DASHBOARD_TOKEN`

```bash
openssl rand -hex 32
```

Put the output into `.env`:

```ini
DASHBOARD_TOKEN=<paste the 64-character hex string here>
```

!!! warning "Empty token = open dashboard"
    If `DASHBOARD_TOKEN` is empty, the auth middleware is bypassed entirely (so local-dev still works without configuration). sembr will log an `ERROR` at startup if it detects an empty token, but **it will still start** — do not ignore that line.

## 3. Bind sembr to loopback only

The shipped `docker-compose.yml` publishes the API on `0.0.0.0:8000` so home / LAN setups work out of the box. For a public-internet host you want the opposite: the API reachable only from the local machine, and the only inbound path the reverse proxy you'll set up in the next step.

### 3a. API service — pick one of two ways

**Option 1 (recommended): set `SEMBR_BIND_ADDR` in `.env`.** The compose file is wired to honour it (`${SEMBR_BIND_ADDR:-0.0.0.0}:${SEMBR_HOST_PORT:-8000}:8000`), so no compose edit is needed:

```ini
# .env
SEMBR_BIND_ADDR=127.0.0.1
```

Then `docker compose up -d --force-recreate api`.

**Option 2: edit `docker-compose.yml` directly** if you prefer that style. Replace the api service's `ports:` line with:

```yaml
  api:
    ports:
      - "127.0.0.1:${SEMBR_HOST_PORT:-8000}:8000"   # hardcoded 127.0.0.1: prefix
```

### 3b. qdrant and rsshub — mandatory compose edit

`qdrant` (6333 / 6334) and `rsshub` (1200) are hardcoded to `0.0.0.0` in `docker-compose.yml`. They do not honour `SEMBR_BIND_ADDR`, and **you cannot rely on ufw to close them** — Docker inserts its own iptables rules into the FORWARD chain (via the `DOCKER` chain) that take precedence over ufw's INPUT rules. A published port on `0.0.0.0` is reachable from the public internet regardless of what `ufw status` shows, unless you also install `ufw-docker` or disable Docker's iptables (which breaks bridge networking).

So edit `docker-compose.yml` and prefix `127.0.0.1:` to each of these three port lines:

```yaml
  rsshub:
    ports:
      - "127.0.0.1:1200:1200"   # was "1200:1200"

  qdrant:
    ports:
      - "127.0.0.1:6333:6333"   # was "6333:6333"
      - "127.0.0.1:6334:6334"   # was "6334:6334"
```

None of these need to be reachable from anywhere off the VM — the API container talks to them over the docker network regardless of the published port.

Then bring them back up with the new bindings:

```bash
docker compose up -d --force-recreate api qdrant rsshub
```

### 3c. Need to hit it from your laptop?

Don't undo the loopback bind — use SSH port-forwarding instead:

```bash
ssh -L 8000:127.0.0.1:8000 user@your.vm.ip
# now hit http://localhost:8000 from your laptop
```

## 4. Set up a reverse proxy with TLS

Pick one. **Caddy is recommended** for first-time deployments — it handles Let's Encrypt automatically with zero certbot machinery.

### Option A: Caddy (recommended)

Install Caddy on the host ([official instructions](https://caddyserver.com/docs/install)), then create `/etc/caddy/Caddyfile`:

```caddyfile
your.domain.com {
    encode gzip zstd

    # Optional belt-and-braces: only allow GET/POST/PUT/DELETE
    @methods {
        not method GET POST PUT DELETE OPTIONS HEAD
    }
    respond @methods 405

    # SSE needs flush_interval so events arrive immediately
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-For {remote_host}
    }

    # Reasonable defaults — sembr will set its own response headers
    # once the SecureHeaders middleware lands; until then these help.
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        Referrer-Policy strict-origin-when-cross-origin
        -Server
    }
}
```

Then:

```bash
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

Caddy fetches a Let's Encrypt cert on first request and renews automatically.

### Option B: nginx + certbot

If you prefer nginx, here's a minimal config (`/etc/nginx/sites-available/sembr`):

```nginx
server {
    listen 80;
    server_name your.domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your.domain.com;

    # certbot manages these two:
    ssl_certificate     /etc/letsencrypt/live/your.domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your.domain.com/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    client_max_body_size 5m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE / streaming
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

Then enable the site, drop the default site to avoid a "duplicate listen 80" conflict (`/etc/nginx/sites-enabled/default` ships enabled on a fresh nginx install and also binds port 80), and run certbot:

```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -s /etc/nginx/sites-available/sembr /etc/nginx/sites-enabled/sembr
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your.domain.com
```

### Option C: Cloudflare Tunnel (no inbound port at all)

The strongest option if you don't want any inbound port open on the VM. Install [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/), authenticate, create a tunnel, route `your.domain.com` → `http://127.0.0.1:8000`. Cloudflare terminates TLS; your firewall can be **deny-all-inbound** except SSH.

!!! warning "SSE limit"
    Cloudflare's edge terminates idle SSE / streaming connections after ~100 s on the free plan. The dashboard's **Logs** tab uses SSE, so it will silently stop updating through a Cloudflare Tunnel. Everything else (intent / feed CRUD, fire, settings, the digest pipeline) works normally. Caddy and nginx don't have this limit — pick A or B if real-time Logs matter.

### Option D: trycloudflare (ephemeral, zero registration)

If you just want sembr reachable on the public internet **right now**, without registering a domain or signing into anything, the simplest path is Cloudflare's anonymous quick tunnel:

```bash
# After Steps 1–3 (token + loopback bind) are done and sembr is up locally:
cloudflared tunnel --url http://127.0.0.1:8000
```

You'll get a URL like `https://xyz-abc.trycloudflare.com` printed to stdout that proxies to your sembr. **It's ephemeral** — restart the tunnel process and the URL changes. Use `nohup cloudflared tunnel --url ... & disown` if you want it to survive your SSH session ending; for survival across reboots, graduate to Option C (proper tunnel) or A/B with a registered domain. Same SSE limit applies as Option C.

## 5. Firewall

!!! danger "ufw does **not** reliably close Docker-published ports"
    Docker inserts its own iptables rules in the `DOCKER` chain (off `FORWARD`) that take precedence over ufw's `INPUT` rules. A service published on `0.0.0.0:N` via compose will answer on the public IP regardless of `ufw deny`. The **only** reliable way to close 8000 / 6333 / 6334 / 1200 is the compose-level loopback bind from step 3 — ufw is for the host's *non-Docker* surface (SSH, anything you installed directly on the metal). Don't skip step 3 thinking ufw will compensate.

Using `ufw` (Ubuntu / Debian default):

**Caddy / nginx (options A and B) — allow 80 + 443:**

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 443/tcp
sudo ufw allow 80/tcp                 # for Let's Encrypt HTTP-01 + redirect
sudo ufw allow from <YOUR_HOME_IP> to any port 22 proto tcp
# OR if your home IP changes:
sudo ufw allow 22/tcp
sudo ufw enable
sudo ufw status verbose
```

**Cloudflare Tunnel / trycloudflare (options C and D) — SSH only, no 80 or 443 needed:**

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp                 # or restrict to your home IP
sudo ufw enable
sudo ufw status verbose
```

The tunnel is an outbound connection from the VM to Cloudflare; no inbound port is required.

## 6. SSH hardening

!!! note "Generic public-VM hygiene, not sembr-specific"
    The items in this section harden the VM you're running sembr on. They're not part of sembr itself and apply identically whatever you're hosting. If your VM is already SSH-hardened (key-only auth, no root login, fail2ban configured), skip to section 7. We include them here because the most common way a sembr deployment falls is through the host's management plane, not through sembr's HTTP surface.

Edit `/etc/ssh/sshd_config`:

```
PermitRootLogin no
PasswordAuthentication no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
UsePAM yes
AllowUsers <your-username>
```

Reload:

```bash
sudo systemctl reload ssh
```

Optional but recommended:

- Install `fail2ban` or `crowdsec` to ban repeated SSH probe IPs.
- Run `unattended-upgrades` (Debian/Ubuntu) so kernel/openssh patches land without manual `apt upgrade`.

## 7. Verify

From a machine **not** on the VM:

```bash
# Health is intentionally auth-free
curl -i https://your.domain.com/health
# → HTTP/2 200

# API is protected — without a token you get 401.
# /intents is the actual gated path; /api/dashboard/* is also gated. There is
# no /api/intents — earlier versions of this doc mistakenly used that path.
curl -i https://your.domain.com/intents
# → HTTP/2 401

# With a wrong token, still 401
curl -i -H "X-Dashboard-Token: nope" https://your.domain.com/intents
# → HTTP/2 401

# With the real token, 200
curl -i -H "X-Dashboard-Token: $DASHBOARD_TOKEN" https://your.domain.com/intents
# → HTTP/2 200

# Every non-public port must be unreachable from outside the VM. 8000 = api,
# 6333/6334 = qdrant, 1200 = rsshub. If ANY of these answers, step 3 wasn't
# applied — see "ufw does not close Docker ports" warning above step 5.
nmap -p 80,443,8000,6333,6334,1200,22 your.domain.com
# → 80/tcp   filtered or 301-redirected to 443 (Caddy/nginx); closed (Cloudflare)
# → 443/tcp  open (Caddy/nginx); closed (Cloudflare Tunnel — TLS is on CF's edge)
# → 8000/tcp filtered or closed   ← critical
# → 6333/tcp filtered or closed   ← critical (Qdrant has no auth)
# → 6334/tcp filtered or closed   ← critical
# → 1200/tcp filtered or closed   ← critical (RSSHub is an SSRF gadget)
# → 22/tcp   open (or filtered, if you restricted it)
```

If `nmap` shows any of 8000 / 6333 / 6334 / 1200 as `open`, you skipped step 3 (or only did 3a, not 3b) — fix the compose port bindings before letting anyone use the public URL.

## 8. Backups

`docker-compose.yml` bind-mounts two host directories that hold all state:

| Path | Contents |
| --- | --- |
| `./data/` | SQLite DB + Qdrant collection + cached articles |
| `./.env` | All your secrets and tokens (treat as crown jewels) |
| `./prompts/` | Custom prompt templates |

Minimal nightly backup with `rsync` to an off-site box:

```bash
# /etc/cron.daily/sembr-backup
#!/bin/bash
set -e
DEST=user@backup.host:/srv/backups/sembr/$(hostname)/$(date +%F)
rsync -az --delete \
    --exclude='.git' --exclude='__pycache__' \
    /opt/sembr/data /opt/sembr/.env /opt/sembr/prompts \
    "$DEST"
```

Encrypt `.env` at rest on the backup host (e.g., `age` or `restic`) — it contains the SiliconFlow / LLM / SMTP keys.

## 9. Monitoring

Lightweight options that work with the existing `/health` endpoint:

- **[UptimeRobot](https://uptimerobot.com)** — free tier polls `/health` every 5 minutes, emails / pings on failure
- **[Uptime Kuma](https://uptime.kuma.pet)** — self-hosted, fancier dashboards
- **[ntfy](https://ntfy.sh) + cron** — quick-and-dirty: `curl -fs https://your.domain.com/health || curl -d "sembr down" ntfy.sh/your-topic`

Check the **SiliconFlow billing dashboard** weekly. Until rate-limit middleware lands (see Known Limits below), a leaked token can rack up embedder costs quickly.

## 10. Known limits (as of v1.0)

These are areas where sembr does **not yet** provide built-in defense. Each will be tracked in a future release; until then, the reverse proxy is the place to enforce them.

| Gap | Mitigation today |
| --- | --- |
| **No per-IP rate limit middleware** — `DASHBOARD_TOKEN` can be brute-forced, and authenticated abusers can run up your embedder bill | Use Caddy's `rate_limit` plugin or nginx `limit_req_zone` against `/api/intents` and `/api/external/*`. Cap at e.g. 60 req/min/IP. |
| **No global body-size limit in app** | Set `client_max_body_size 5m;` in nginx, or `request_body { max_size 5MB }` in Caddy |
| **Sembr container runs as root** | Don't mount additional host volumes beyond what compose ships with. |
| **Docker socket mounted into the API container** — needed for the in-app "restart RSSHub" button; means compromising the API ≈ root on the host | **For public deployments the recommended default is to comment out the `/var/run/docker.sock:/var/run/docker.sock` mount** in `docker-compose.yml`. Without the socket the api container's blast radius is bounded; with it, an api-container RCE (or a stolen `DASHBOARD_TOKEN` plus any code path that touches the docker API) escalates to host root. You only lose the dashboard's "Restart RSSHub" button — `docker compose restart rsshub` from SSH still works. Keep the mount only if you actively rely on the button. |
| **No 2FA / SSO** — single shared `DASHBOARD_TOKEN` for all admins | Rotate the token if anyone with access leaves. Treat it like an SSH key. |
| **`/health` is unauthenticated** — by design, so monitors can poll without holding a token | This endpoint returns liveness only; no sensitive data leaks through it. |

## 11. Reporting security issues

If you find a vulnerability in sembr itself (not in your deployment), please **do not** open a public GitHub issue. Use GitHub's [Private Vulnerability Reporting](https://github.com/Peakstone-Labs/sembr/security/advisories/new) instead. See [`SECURITY.md`](https://github.com/Peakstone-Labs/sembr/blob/main/SECURITY.md) for full details.
