# WeChat Official Accounts (add-on)

This guide shows how to ingest **WeChat Official Account** (微信公众号) articles into sembr as an
optional add-on. WeChat has no official feed API, so this relies on a **self-hosted, third-party
bridge** ([wechat2rss](https://wechat2rss.xlab.app/)) that turns Official Account articles into
full-text RSS. sembr then consumes that RSS through its existing feed support — **no sembr code
change, no new source type**.

!!! warning "Optional, third-party, and your responsibility"
    - sembr does **not** bundle, ship, or endorse the bridge — you deploy and operate it yourself.
    - wechat2rss self-hosting requires a **paid license** and a **dedicated WeChat account** you supply
      (use a secondary account, not your primary one — account-ban risk is real and yours to accept).
    - You are responsible for complying with WeChat's Terms of Service in your jurisdiction.
    - There is **no official API**; this is a community workaround whose reliability depends on the
      upstream bridge.

!!! note "Why a bridge at all?"
    WeChat's ecosystem is closed — there is no public, official way to subscribe to an arbitrary
    Official Account. A bridge logs in with a real WeChat account and re-publishes followed accounts as
    RSS. Once it's RSS, sembr treats it exactly like any other RSS feed (de-dup, full-text extraction,
    embedding, intent matching, digests).

---

## TL;DR (6-step checklist)

1. Buy a **wechat2rss** self-host license.
2. Deploy the **wechat2rss** container (`docker compose up -d`).
3. Open its admin UI and **scan-login a dedicated WeChat account**.
4. **Subscribe** the Official Accounts you want (paste an article link per account); copy each feed URL.
5. Put wechat2rss and sembr on a **shared Docker network** so sembr can reach it by container name.
6. **Add an `rss` feed** in sembr pointing at the bridge URL.

The rest of this page walks through each step.

---

## 1. Prerequisites

- A running sembr deployment (Docker Compose).
- A **wechat2rss self-host license** — see [wechat2rss.xlab.app](https://wechat2rss.xlab.app/)
  (sold as a software license; it does not include a hosted service).
- A **dedicated, non-primary WeChat account** to bind as the bridge's read source. Its login session
  expires periodically (on the order of a few days) and must be re-scanned — plan for that.
- Docker + Docker Compose on the same host as sembr (recommended for the simplest networking).

## 2. Deploy wechat2rss

Create `~/wechat2rss/docker-compose.yml`:

```yaml
services:
  wechat2rss:
    container_name: wechat2rss
    image: "ttttmr/wechat2rss:latest"
    env_file:
      - .env
    volumes:
      - ./data:/wechat2rss          # persists login session, settings, article cache
    ports:
      - "8080:8080"                 # admin UI + feeds; bind to 127.0.0.1:8080 if you only tunnel in
    networks:
      - wechat2rss-net              # shared network so sembr can reach it by name (see §3)
    deploy:
      restart_policy:
        condition: on-failure
        max_attempts: 3
        window: 10s
    logging:
      driver: "json-file"
      options:
        max-size: "100m"
networks:
  wechat2rss-net:
    external: true                  # created in §3
```

Create `~/wechat2rss/.env` (and `chmod 600` — never commit it):

```ini
LIC_EMAIL=<license email>
LIC_CODE=<license code>
RSS_HOST=<host:8080>        # only affects the subscription URL shown in the UI
# Optional re-login / health alerts (pick one). Without these the bridge fails silently when its
# login expires, and your WeChat feeds quietly stop updating:
# BOT_SERVER_KEY=<serverchan key>
# BOT_TG_TOKEN=<telegram bot token>   BOT_TG_ADMIN_UID=<your uid>
# BOT_WEBHOOK_URL=<webhook>           BOT_BARK_URL=<bark url>
```

Start it and confirm the license check passed:

```bash
cd ~/wechat2rss && docker compose up -d
docker compose logs --tail=50            # look for the license "Expire" line and the admin "Token"
```

!!! warning "Login expires; wire up an alert"
    The bound WeChat session expires every few days and must be re-scanned. Configure one of the
    `BOT_*` alert channels above so the bridge **tells you** when a re-login is needed instead of
    silently going stale. (sembr's own Feeds tab will also show the affected feeds flat-lining, but
    that is a lagging signal.)

### Bind the account and subscribe

1. Reach the admin UI. If you bound the port to `127.0.0.1`, tunnel in:
   `ssh -L 8080:localhost:8080 <host>` then open `http://localhost:8080`. Enter the admin token from
   the startup logs.
2. **WeChat accounts → Add account → scan the QR with your dedicated WeChat account.** Complete any
   login-verification prompt in the page.
3. **Subscribe**: paste any article link (`https://mp.weixin.qq.com/s/...`) from each Official Account
   you want. On success the UI shows that account's feed URL, of the form
   `http://<RSS_HOST>/feed/<biz_id>.xml`.

## 3. Connect the bridge to sembr (production-grade)

sembr's API runs in a container, so it must reach wechat2rss **by container name over a shared Docker
network** — not via `host.docker.internal`, which only exists on Docker Desktop and is not portable to
Linux Docker Engine.

Create an external network and attach both stacks to it (the wechat2rss compose above already
declares it):

```bash
docker network create wechat2rss-net
```

sembr's `docker-compose.yml` lives in the repo and is overwritten by `git pull`, so don't edit it
directly. Instead add a **local** `docker-compose.override.yml` next to it — Docker Compose merges it
automatically:

```yaml
# docker-compose.override.yml — local add-on overlay; keep it out of git.
services:
  api:
    networks:
      - default            # keep the default network, or the API loses qdrant / rsshub
      - wechat2rss-net
networks:
  wechat2rss-net:
    external: true
```

Keep it untracked so `git pull` never clobbers it:

```bash
grep -qxF docker-compose.override.yml .git/info/exclude || echo docker-compose.override.yml >> .git/info/exclude
docker compose up -d          # recreates the api container on the shared network
```

Verify the API container can reach the bridge by name:

```bash
docker exec sembr-api python -c \
  "import urllib.request as u; print(u.urlopen('http://wechat2rss:8080/feed/<biz_id>.xml', timeout=10).status)"
# 200
```

## 4. Add the feed to sembr

A WeChat feed is just an **RSS feed** — no new source type. Use the bridge's **container-name** URL.
Add it from the dashboard **Feeds** tab, or via the API:

```bash
curl -X POST http://127.0.0.1:8000/feeds \
  -H "X-Dashboard-Token: <DASHBOARD_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<account name>",
    "url": "http://wechat2rss:8080/feed/<biz_id>.xml",
    "source_type": "rss",
    "poll_interval_minutes": 120,
    "tags": ["wechat"]
  }'
```

sembr's RSS collector extracts the **full article text** and strips HTML automatically (it treats
WeChat feeds exactly like any other RSS source), so no extra cleanup is needed. Tagging the feeds
(`"tags": ["wechat"]`) makes them easy to manage in bulk.

## 5. Notes and limitations

- **Two-layer polling.** Your sembr `poll_interval_minutes` controls how often sembr pulls the bridge's
  RSS endpoint (which serves a local cache — cheap). It does **not** control freshness: how fast new
  articles appear is set by the bridge's own crawl cadence (typically a few hours, up to ~24h). Polling
  sembr more often than that just re-fetches the same cache (de-dup discards it). **60–120 minutes is a
  sensible sembr interval.**
- **Long articles are truncated for embedding.** sembr caps embedding input (~8000 chars) to stay within
  the embedder's token budget. Very long Official Account research posts are embedded on their leading
  portion; the full text is still stored.
- **Image-only posts carry little text.** "一图读懂" / poster-style articles put their content in images,
  so the extracted text — and therefore semantic matching quality — is thin. That's a content-format
  limitation, not a bug.
- **LAN access trade-off.** Binding the admin UI to `0.0.0.0:8080` makes it reachable on your LAN
  (convenient for re-login from any device); the admin UI is token-gated, but the feed URLs are readable
  by anyone on the LAN. Bind to `127.0.0.1` and tunnel if you'd rather keep it private.
- **License renewal.** If the wechat2rss license lapses, the bridge stops and your WeChat feeds go
  silent — set a reminder before expiry.

---

→ Running sembr on a public IP? See [Public server](public.md) for the hardening checklist.
→ New to sembr feeds? Start with [Getting Started](../getting-started.md).
