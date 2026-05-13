# Deploying sembr on a Public-Facing Server

This guide is for users who want to run sembr on a cloud VM (DigitalOcean / Hetzner / Vultr / EC2 / …) and reach the dashboard from anywhere on the internet.

!!! danger "Default config is **not** public-internet safe"
    Out of the box, sembr binds to `0.0.0.0:8000` in plaintext with `DASHBOARD_TOKEN` empty. If you `docker compose up` on a VM with a public IP and open port 8000 in the firewall, **the dashboard is open to the world with no authentication**. This default is chosen to keep LAN / home-server setups frictionless; the steps below walk you through the changes you must make before exposing sembr publicly.

The recommended path is still a private network (Tailscale / WireGuard / VPN) — see [Getting Started](../getting-started.md). Use this guide only if you actually need a public endpoint.

---

## TL;DR (the 6-step checklist)

1. Set a strong **`DASHBOARD_TOKEN`** in `.env`.
2. Edit `docker-compose.yml` to bind the API to **loopback only** (`127.0.0.1:8000:8000`).
3. Put it behind a **reverse proxy with TLS** (Caddy is the easiest).
4. **Firewall**: allow 443, drop 8000, lock 22 down to your SSH key.
5. **SSH hardening**: disable password auth and root login.
6. **Verify** with `curl` and `nmap` from a different network.

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

Edit the `api` service's `ports:` line in `docker-compose.yml`:

```yaml
  api:
    ports:
      - "127.0.0.1:${SEMBR_HOST_PORT:-8000}:8000"   # add the 127.0.0.1: prefix
```

While you're at it, do the same for `qdrant` (`6333:6333` / `6334:6334`) and `rsshub` (`1200:1200`) — none of them need to be reachable from the public internet, and the API container talks to them over the docker network regardless of the published port.

Then `docker compose up -d --force-recreate api qdrant rsshub`.

Need to test from another machine? Don't undo this — use SSH port-forwarding instead:

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

Then:

```bash
sudo ln -s /etc/nginx/sites-available/sembr /etc/nginx/sites-enabled/sembr
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your.domain.com
```

### Option C: Cloudflare Tunnel (no inbound port at all)

The strongest option if you don't want any inbound port open on the VM. Install [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/), authenticate, create a tunnel, route `your.domain.com` → `http://127.0.0.1:8000`. Cloudflare terminates TLS; your firewall can be **deny-all-inbound** except SSH.

## 5. Firewall

Using `ufw` (Ubuntu / Debian default):

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

!!! tip "Cloudflare Tunnel users"
    With Option C above, you can `sudo ufw default deny incoming` and only allow SSH — no port 80/443 needed.

## 6. SSH hardening

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

# API is protected — without a token you get 401
curl -i https://your.domain.com/api/intents
# → HTTP/2 401

# With a wrong token, still 401
curl -i -H "X-Dashboard-Token: nope" https://your.domain.com/api/intents
# → HTTP/2 401

# With the real token, 200
curl -i -H "X-Dashboard-Token: $DASHBOARD_TOKEN" https://your.domain.com/api/intents
# → HTTP/2 200

# Port 8000 should NOT be reachable from outside the VM
nmap -p 80,443,8000,22 your.domain.com
# → 80/tcp filtered or 301-redirected to 443
# → 443/tcp open
# → 8000/tcp filtered or closed   ← critical
# → 22/tcp open (or filtered, if you restricted it)
```

If `nmap` shows port 8000 `open`, you skipped step 3 — fix the compose `ports:` binding before going any further.

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
| **Docker socket mounted into the API container** — needed for the in-app "restart RSSHub" button; means compromising the API ≈ root on the host | If you don't use the RSSHub auto-restart feature, **comment out the `/var/run/docker.sock` mount** in `docker-compose.yml`. The feature degrades gracefully. |
| **No 2FA / SSO** — single shared `DASHBOARD_TOKEN` for all admins | Rotate the token if anyone with access leaves. Treat it like an SSH key. |
| **`/health` is unauthenticated** — by design, so monitors can poll without holding a token | This endpoint returns liveness only; no sensitive data leaks through it. |

## 11. Reporting security issues

If you find a vulnerability in sembr itself (not in your deployment), please **do not** open a public GitHub issue. Use GitHub's [Private Vulnerability Reporting](https://github.com/Peakstone-Labs/sembr/security/advisories/new) instead. See [`SECURITY.md`](https://github.com/Peakstone-Labs/sembr/blob/main/SECURITY.md) for full details.
