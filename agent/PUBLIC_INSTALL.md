# sembr — Agent-Driven Public-Exposure Sub-Flow

> **This document is written for an AI agent** (OpenClaw, Hermes, Claude Code or similar). You should reach this doc as a **branch from [`INSTALL.md`](INSTALL.md) Phase 4** when the user picked **option C (public internet)** in the access-mode question. Complete the steps below, then return to `INSTALL.md` Phase 5 to bring the stack up.
>
> Scope: setting up everything between the public internet and `127.0.0.1:${SEMBR_HOST_PORT}`. That means DNS, side-service port lockdown in `docker-compose.yml`, reverse proxy + TLS, and the host firewall.
>
> Out of scope (you don't do these — the user does or has done them): general SSH hardening, OS patching, fail2ban, choice of cloud provider, billing alerts, off-host backup plumbing. These are VM hygiene, not sembr-specific. We trust the operator's existing setup.

## State you are arriving in

If you came through `INSTALL.md` Phases 1–4 with option C, the following is already true on this host. Don't redo it.

- `${SEMBR_DIR}/.env` exists with:
  - `EMBEDDER_API_KEY=` and `LLM_API_KEY=` filled in
  - `SEMBR_BIND_ADDR=127.0.0.1` (api will publish on loopback only when the stack starts)
  - `DASHBOARD_TOKEN=` set to a 64-char hex value
- The stack is **not yet running** — `INSTALL.md` Phase 5 hasn't executed. That is the right state: we want DNS, the reverse proxy, and the firewall in place before binding any external surface.
- You hold `${SEMBR_DIR}` and `${PORT}` as shell variables. If you opened a fresh shell, re-export: `SEMBR_DIR=...`, `PORT=$(grep -E '^SEMBR_HOST_PORT=' "${SEMBR_DIR}/.env" | cut -d= -f2); PORT=${PORT:-8000}`.

If any of the above isn't true, stop and finish `INSTALL.md` Phases 1–4 first.

## How to use this document

Same conventions as `INSTALL.md`:

| Block | Meaning |
| --- | --- |
| **Agent** | Shell commands you run yourself and inspect the output of |
| **Tell user** | Information you surface to the user |
| **Ask user** | A blocking question; wait for the user's reply before continuing |
| **Pass / On failure** | The condition that ends the step, and what to do if it doesn't hold |

**Speak the user's language.** Translate Tell-user / Ask-user blocks into whatever language the user has been chatting with you in.

**Consent before privileged action.** Every `sudo` must be surfaced verbatim and approved before you run it. `ufw enable` in particular has permanent consequences — a wrong rule order can lock the agent out of SSH mid-session.

**Order matters.** Steps 1 → 4 must run in sequence. In particular, don't enable `ufw` (Step 4) before the reverse proxy is listening (Step 3), and don't tell the user "go back to Phase 5" until ufw is happy.

---

## Step 1 — Domain and DNS

### 1.1 Confirm this really is a public VM

**Agent:**

```bash
# Public-facing IPv4 of this box. ifconfig.me is an outbound HTTPS lookup; doesn't expose anything new.
PUBLIC_IP=$(curl -sf -m 5 https://ifconfig.me 2>/dev/null || curl -sf -m 5 https://api.ipify.org 2>/dev/null || echo "")
echo "public IP: ${PUBLIC_IP:-<could not detect>}"

# Anything already listening on 80 / 443? (no sudo, no -p — port numbers are all we need)
ss -ltn | awk '$4 ~ /:(80|443)$/'
```

**Pass conditions:**
- `${PUBLIC_IP}` is a public address (not RFC1918, not `127.0.0.1`). If empty, **ask user** to paste the VM's public IP and store it as `${PUBLIC_IP}`.
- Nothing on 80 or 443. If something else is on those ports, ask the user what's running there before proceeding.

**On failure — RFC1918 detected:** this isn't a public VM. The user probably meant LAN (option B). Surface the mismatch; ask whether to back out to LAN mode (clear `SEMBR_BIND_ADDR` from `.env` and skip the rest of this guide; return to `INSTALL.md` Phase 5).

### 1.2 Domain name and A record

**Ask user:**

> "What domain (or subdomain) should sembr live at? Example: `sembr.your-domain.com`. I'll need this for the TLS certificate. If you don't have one yet, you can register one in 10 minutes at Cloudflare / Namecheap / Porkbun. Paste the domain when ready."

Store as `${DOMAIN}`.

**Agent — verify DNS A record points here:**

```bash
DOMAIN="<the user's answer>"

# getent is in glibc — always present, no package install. Use it as primary.
# (dig from `dnsutils` is nicer output but optional.)
RESOLVED=$(getent hosts "${DOMAIN}" | awk '{print $1}' | head -1)
echo "domain ${DOMAIN} → ${RESOLVED:-<not resolving yet>}"
echo "this VM public IP → ${PUBLIC_IP}"

if [ -z "${RESOLVED}" ]; then
  echo "  ✗ no resolution yet"
elif [ "${RESOLVED}" = "${PUBLIC_IP}" ]; then
  echo "  ✓ DNS matches"
else
  echo "  ✗ DNS points at ${RESOLVED}, expected ${PUBLIC_IP}"
fi
```

**Pass condition:** `${RESOLVED}` equals `${PUBLIC_IP}`.

**On failure — no record / wrong record:**

**Tell user:**

> "DNS isn't pointed at this VM yet. In your domain provider's DNS panel, create:
> - **Type:** A
> - **Name:** `<subdomain>` (e.g. `sembr`, or `@` if `${DOMAIN}` is the apex)
> - **Value:** `${PUBLIC_IP}`
> - **TTL:** 5 min or default
>
> If your domain is on Cloudflare, set the **proxy** toggle to **DNS only** (grey cloud) for now — orange cloud changes the source IP we'd see during TLS challenge and breaks Let's Encrypt HTTP-01. You can flip it on later if you prefer Cloudflare in front.
>
> Propagation usually completes in a minute; up to 30 in the worst case. Tell me when you've added the record and I'll re-check."

Loop `getent hosts "${DOMAIN}"` every 30 s for up to 30 min until the result matches `${PUBLIC_IP}`. If it doesn't resolve in time, surface the diff and ask the user to double-check the DNS panel.

### 1.3 Pick the TLS / reverse-proxy strategy

**Tell user:**

> "Three options. Pick one:
>
> | Option | Best for | Trade-off |
> | --- | --- | --- |
> | **A. Caddy** (recommended) | First-time deployments; no certbot machinery | Caddy handles Let's Encrypt automatically; one ~15-line `Caddyfile` |
> | **B. nginx + certbot** | You already run nginx, or want to integrate with an existing nginx config | More steps, more knobs; well-trodden |
> | **C. Cloudflare Tunnel** | You want **no inbound port open** on the VM (deny-all firewall + SSH only); fine with Cloudflare being in front | Requires a Cloudflare account; CF terminates TLS, not you |
>
> Default: **A**. Pick A unless you have a specific reason."

**Ask user:** "A / B / C?" — store as `${TLS_OPTION}`.

---

## Step 2 — Lock down the side services (mandatory)

`docker-compose.yml` honours `SEMBR_BIND_ADDR` for the api service, but `qdrant` (6333 / 6334) and `rsshub` (1200) are hardcoded to `0.0.0.0`. On a LAN box that's fine; on a public VM it's a serious problem — Qdrant has no auth by default and RSSHub is a willing SSRF gadget.

**You cannot rely on ufw to close these ports**, because Docker inserts its own iptables rules into the FORWARD chain (via the `DOCKER` chain) that take precedence over ufw's INPUT rules. A published port on `0.0.0.0` will be reachable from the public internet regardless of what `ufw status` says, unless you also install `ufw-docker` or disable Docker's iptables (which breaks bridge networking). The clean fix is to bind at the compose level.

This is the one place where `INSTALL.md`'s "don't touch compose" rule deliberately bends. Show the diff to the user before applying.

**Tell user:**

> "I need to bind Qdrant and RSSHub to loopback too — they currently publish on `0.0.0.0` regardless of `SEMBR_BIND_ADDR`, and ufw cannot reliably close Docker-published ports. Four lines in `docker-compose.yml`:
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

**Ask user:** "Apply? (yes / no)"

If **no** — stop. Without this edit the deployment is not safe to expose publicly; ask whether to abort to LAN mode (option B in Phase 4).

**Agent:**

```bash
cd "${SEMBR_DIR}"
cp docker-compose.yml docker-compose.yml.bak

# Note: this sed pattern matches the exact "  - "PORT:PORT"" formatting in
# the shipped compose file. If the file has been hand-edited, the match may
# silently no-op — verify the diff after.
sed -i 's|^\(\s*-\s\+\)"1200:1200"|\1"127.0.0.1:1200:1200"|; \
        s|^\(\s*-\s\+\)"6333:6333"|\1"127.0.0.1:6333:6333"|; \
        s|^\(\s*-\s\+\)"6334:6334"|\1"127.0.0.1:6334:6334"|' docker-compose.yml

diff docker-compose.yml.bak docker-compose.yml || true
```

**Pass condition:** the diff shows exactly three lines changing, each gaining a `127.0.0.1:` prefix. If you see zero or one line changed, the regex missed — inspect the file by eye and ask the user before retrying.

(The stack isn't running yet, so no `docker compose up` here. The changes take effect when `INSTALL.md` Phase 5 brings everything up.)

---

## Step 3 — Reverse proxy + TLS

### 3.A — Caddy (recommended)

**Ask user:** "Install Caddy via the official apt repo (https://caddyserver.com/docs/install)? Proceed?"

**Agent:**

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

**Pass condition:** `sudo journalctl -u caddy --since '2 min ago' --no-pager | tail -20` shows `certificate obtained successfully` for `${DOMAIN}`. Caddy fetches the Let's Encrypt cert on first reload (or within ~60 s).

The stack isn't up yet, so `curl https://${DOMAIN}/health` will return 502 — that's expected. The proxy and cert are what we're validating here.

**On failure — "no such host" / DNS resolution error:** DNS hasn't propagated. Go back to Step 1.2 and wait longer.

**On failure — "challenge failed" / port 80 unreachable:** the cloud provider's network firewall (security group, not ufw) is blocking 80. Tell the user to open 80 and 443 inbound in their cloud console (AWS Security Group, DigitalOcean Firewall, GCP firewall rule, etc.) before retrying.

### 3.B — nginx + certbot

**Ask user:** "What email should Let's Encrypt use for expiration notices? I need this before running certbot. Use `${SMTP_FROM:-<the user's email>}` or paste a different one." — store as `${LE_EMAIL}`.

**Ask user:** "Install nginx and certbot? Proceed?"

**Agent — install:**

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

# A fresh nginx install enables /etc/nginx/sites-enabled/default which also
# binds port 80. Two server blocks on the same port = nginx -t fails with
# "duplicate listen". Drop the default before enabling sembr.
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/sembr /etc/nginx/sites-enabled/sembr

# Temporarily comment out the ssl_* lines so nginx -t passes before certbot runs.
sudo sed -i.bak '/ssl_certificate/s/^/# /' /etc/nginx/sites-available/sembr
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${LE_EMAIL}"

# certbot un-comments the ssl_certificate lines automatically; verify and reload.
sudo nginx -t && sudo systemctl reload nginx
```

**Pass condition:** `curl -sI -m 10 https://${DOMAIN}/` returns a TLS-terminated response (the status will be 502 because sembr isn't up yet — that's fine; we only care that the cert is live and the proxy is forwarding).

### 3.C — Cloudflare Tunnel

**Ask user:** "Cloudflare Tunnel needs (1) a Cloudflare account with `${DOMAIN}`'s zone already added, and (2) you to authenticate `cloudflared` interactively in a browser. I can install `cloudflared` and prepare the config, but you'll have to run `cloudflared tunnel login` yourself. Proceed?"

**Agent — install `cloudflared`:**

```bash
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
      # Origin (sembr) is on loopback over plain HTTP — TLS is between the
      # client and Cloudflare's edge, not between cloudflared and sembr.
      noTLSVerify: true
      disableChunkedEncoding: false
  - service: http_status:404
EOF

cloudflared tunnel route dns "${TUNNEL_UUID}" "${DOMAIN}"
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Cloudflare adds a CNAME for you; no manual A-record needed (the one you set in Step 1.2 can stay or be deleted, your call).

**Pass condition:** `systemctl is-active cloudflared` returns `active`. `curl https://${DOMAIN}/` will 502 until sembr is up — that's expected.

---

## Step 4 — Firewall (ufw)

ufw doesn't reliably restrict Docker-published ports (Step 2 covered those at the compose layer), but it does close every *non-Docker* listening port and gives you a clean "only 22 / 80 / 443" deny-all-else stance on the host side.

If the user is on a non-Debian/Ubuntu distro or already runs `firewalld` / a hand-rolled `iptables` setup, ask before clobbering.

**Ask user:**

> "Set up `ufw` with: allow 22 (SSH), allow 80 and 443 (reverse-proxy traffic), deny everything else inbound?
>
> ⚠️ Once ufw is enabled, the SSH rule must already be in place — otherwise this session can drop. If you're unsure whether you SSH from a fixed IP, leave 22 open from anywhere (`sudo ufw allow 22/tcp`) and tighten later. Proceed?"

**Agent:**

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing

sudo ufw allow 22/tcp comment 'ssh'

# Caddy / nginx branches need 80 (HTTP-01 + redirect) and 443.
# Cloudflare Tunnel branch: no inbound port besides SSH.
if [ "${TLS_OPTION}" != "C" ]; then
  sudo ufw allow 80/tcp  comment 'http-01 + redirect'
  sudo ufw allow 443/tcp comment 'sembr https'
fi

sudo ufw --force enable
sudo ufw status verbose
```

**Pass condition:** `ufw status` shows the rules, and SSH still works.

**On failure — you lost your SSH session:** the user has to log in via the cloud provider's web console / serial console and run `sudo ufw disable`. Surface this risk *before* enabling, every time.

---

## Step 5 — Decide on the docker-socket bind-mount

The shipped `docker-compose.yml` bind-mounts `/var/run/docker.sock` into the api container so the dashboard's **"Restart RSSHub"** button can issue a `docker restart` against the host. Docker socket inside a container is equivalent to root on the host — an attacker who reaches code execution in the api container (or holds a stolen `DASHBOARD_TOKEN` and exploits any code path that touches the docker API) can spawn a privileged container with the host filesystem mounted, and from there read SSH keys, write `cron` jobs, escalate to host root.

For a **public-internet deployment** this is a serious blast-radius amplifier. The api container only needs to be compromised once for the whole host to fall. The cost of removing the mount: operator restarts RSSHub manually with `docker compose restart rsshub` from a shell instead of clicking a button.

**Tell user:**

> "The api container has `/var/run/docker.sock` mounted so the dashboard's 'Restart RSSHub' button works. On a public VM that's a meaningful risk — if the api ever gets compromised, the attacker uses the socket to escalate to host root. **I strongly recommend disabling it** for public deployments; you'll lose only the button (manual `docker compose restart rsshub` from SSH still works). Disable?"

**Ask user:** "yes (recommended) / no / decide later"

If **yes**:

```bash
cd "${SEMBR_DIR}"
# Idempotent — comment the bind-mount line. Safe to re-run.
sed -i.bak 's|^\(\s*\)\(- /var/run/docker.sock:/var/run/docker.sock\)|\1# \2|' docker-compose.yml
grep -n 'docker.sock' docker-compose.yml | head -3   # confirm it's now commented
rm -f docker-compose.yml.bak
```

If **no** or **decide later**: surface the trade-off again in the final hand-off so the user remembers.

---

## Done — return to `INSTALL.md` Phase 5

At this point:

- DNS resolves `${DOMAIN}` to this VM (Step 1)
- `docker-compose.yml` binds qdrant + rsshub to loopback; api will bind to loopback via `SEMBR_BIND_ADDR` (Step 2)
- Reverse proxy is installed, configured for `${DOMAIN}`, and (Caddy/nginx) holds a Let's Encrypt cert (Step 3)
- `ufw` is up with 22 / 80 / 443 only, or 22 only for Cloudflare Tunnel (Step 4)
- `docker.sock` mount decision is recorded (Step 5)

**Tell user:**

> "Public-exposure setup is done. Returning to `INSTALL.md` Phase 5 — that will bring up the sembr stack (`docker compose up -d`) and verify `/health` on loopback. Once it's green, Phase 5's branch-C addendum will probe `https://${DOMAIN}` and the still-public ports (8000 / 6333 / 6334 / 1200) from the VM to confirm nothing slipped through."

Go back to `INSTALL.md` Phase 5 and run it normally. The reverse proxy will start serving the running sembr automatically once Phase 5 brings it up.

---

## Out-of-scope follow-ups (Tell user, don't automate)

These are operator responsibilities, not sembr-specific. Surface them once at the end so the user knows they're on the hook.

**Tell user:**

> "Public VM hygiene I'm **not** going to set up for you — these are standard ops choices and you probably have opinions or existing tooling:
>
> - **SSH hardening** — key-only auth, disable root login. (`/etc/ssh/sshd_config`: `PasswordAuthentication no`, `PermitRootLogin no`.) If you haven't done this already on a public VM, do it now from a second SSH session before closing the current one.
> - **Backups** — back up `${SEMBR_DIR}/data/` (SQLite + Qdrant state), `${SEMBR_DIR}/.env` (encrypt at rest off-host — it holds your SiliconFlow key, `DASHBOARD_TOKEN`, SMTP password), and `${SEMBR_DIR}/prompts/` (custom templates). Restic / rclone / rsync over SSH all work. Cron the script after you've confirmed passwordless SSH or an object-storage credential to the destination.
> - **Uptime monitor** — point UptimeRobot / Uptime Kuma / ntfy at `https://${DOMAIN}/health` (intentionally unauthenticated for this purpose).
> - **SiliconFlow billing alert** — sembr 1.0 has no per-IP rate-limit middleware yet. If `DASHBOARD_TOKEN` ever leaks, an authenticated abuser can run up your embedder bill. Set a daily cost alert in the SiliconFlow console.
> - **OS patching** — `unattended-upgrades` on Debian/Ubuntu keeps the kernel and OpenSSH patched without manual `apt upgrade`."

---

## Known limits (v1.0)

| Gap in sembr today | Mitigation on this host |
| --- | --- |
| No per-IP rate-limit middleware | Add Caddy's `rate_limit` plugin or `limit_req_zone` in nginx against `/api/intents` and `/api/external/*`, cap ~60 req/min/IP. Not configured by this guide. |
| No global request-body size limit in the app | `client_max_body_size 5m` is in the nginx config above; Caddy default is 10 MiB. |
| api container runs as root | Don't add bind-mounts beyond what `docker-compose.yml` ships with. |
| Single shared `DASHBOARD_TOKEN` (no 2FA / SSO) | Rotate if anyone with access leaves; treat like an SSH key. |
| `/health` unauthenticated | By design — monitors don't need the token. Returns liveness only; no leak. |

---

## Troubleshooting matrix

| Symptom | Most likely cause | Fix |
| --- | --- | --- |
| `getent hosts ${DOMAIN}` returns nothing | DNS not propagated yet, or wrong A record | Wait 5–30 min; re-check the DNS panel |
| Caddy logs `tls: trying to obtain certificate but it has no SANs` | DNS hadn't resolved when Caddy tried | Re-run `sudo systemctl reload caddy` after DNS resolves |
| `certbot` says `port 80 not reachable` | Cloud provider network firewall (separate from ufw) blocks 80 | Open 80 / 443 inbound in the cloud console (Security Group / network ACL) |
| `nginx -t` says "duplicate listen 80" | Default site still enabled | `sudo rm -f /etc/nginx/sites-enabled/default && sudo nginx -t` |
| External probe shows 6333 / 6334 / 1200 open | Step 2's sed didn't match (custom compose formatting) | Inspect `docker-compose.yml` by eye; add `127.0.0.1:` to those `ports:` entries manually |
| External probe shows 8000 open | `SEMBR_BIND_ADDR` not set to `127.0.0.1` | `grep SEMBR_BIND_ADDR ${SEMBR_DIR}/.env`; should be `127.0.0.1`; if not, fix and `docker compose up -d --force-recreate api` |
| `ufw enable` dropped the agent's SSH session | The `allow 22/tcp` rule wasn't applied before `--force enable` | Use cloud provider's web/serial console, `sudo ufw disable`, fix |
| Caddy says `cannot bind: address already in use` for 80 | Another service is on 80 | `ss -ltn \| awk '$4 ~ /:80$/'`; stop that service |
| Cloudflare Tunnel "connection refused to origin" | sembr stack not up, or wrong port | Run `INSTALL.md` Phase 5; then `curl -s http://127.0.0.1:${PORT}/health` from the VM |

---

## What NOT to do (agent guardrails)

- **Don't** `sudo ufw default deny incoming` without first adding `sudo ufw allow 22/tcp` in the same session.
- **Don't** bind any new public port on this host. sembr's only public surface is 443 (or none, in Cloudflare Tunnel mode).
- **Don't** put `${DASHBOARD_TOKEN}` into a Caddyfile / nginx config / `cloudflared` config. It lives in `.env` only; reverse proxies don't need it.
- **Don't** suggest disabling TLS to "simplify." HTTP-only on a public host leaks `DASHBOARD_TOKEN` on the wire.
- **Don't** mint a new `DASHBOARD_TOKEN` here — the user has the one from `INSTALL.md` Phase 4 written down already.
- **Don't** publish `data/` or `.env` to anywhere world-readable in any backup setup you suggest. `.env` holds the SiliconFlow key, SMTP password, and `DASHBOARD_TOKEN` — crown jewels.
- **Don't** un-comment the `docker.sock` mount later if Step 5 told you to comment it out.
- **Don't** treat SSH-hardening / fail2ban / OS patching / backups as your job. Point them out at the end (the "out-of-scope follow-ups" block) and trust the operator.

---

## Reporting security issues

If you find a vulnerability in sembr itself (not in a deployment), report via GitHub's [Private Vulnerability Reporting](https://github.com/Peakstone-Labs/sembr/security/advisories/new). See [`SECURITY.md`](https://github.com/Peakstone-Labs/sembr/blob/main/SECURITY.md) for details.

---

## Versioning

This guide tracks sembr `main`. For a specific version, prefix the URL with the tag: `https://github.com/Peakstone-Labs/sembr/blob/v1.0.0/agent/PUBLIC_INSTALL.md`.
