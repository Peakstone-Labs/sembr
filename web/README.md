# sembr dashboard frontend

Plain HTML + Alpine.js + Chart.js. **Zero build toolchain**: no `package.json`, no
Node, no bundler. Edit a `.js` / `.html` / `.css` file, refresh the browser,
done.

## Layout

```
web/static/
  index.html   # main dashboard, Alpine x-data="dashboard()"
  login.html   # token entry page (used only when DASHBOARD_TOKEN is set)
  style.css
  app.js       # Alpine component + Chart.js initialization + polling
  vendor/
    alpine.min.js       # Alpine.js v3, vendored from cdn.jsdelivr.net
    chart.umd.min.js    # Chart.js v4, vendored from cdn.jsdelivr.net
```

The vendored files are checked in deliberately. CI / `docker compose build` does
not need network access to assemble the bundle. They are flagged
`linguist-vendored` in `.gitattributes` so they don't pollute language stats.

## Refreshing the vendored libraries

```bash
mkdir -p web/static/vendor
curl -sSL -o web/static/vendor/alpine.min.js \
    https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js
curl -sSL -o web/static/vendor/chart.umd.min.js \
    https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js
```

When you bump versions, smoke-test in a browser: the dashboard renders, the
embedder latency bar chart updates after the first poll, and the drill-down
drawer works for at least one feed and the embedder.

## Disabling the dashboard

If `web/static/index.html` is missing at startup, `sembr/main.py` skips the
`StaticFiles` mount and logs an INFO line. The JSON API at `/api/dashboard/*`
remains available — useful for CLI tooling or a custom UI built on top.

## Authentication

The dashboard checks `/api/dashboard/config` (auth-free) on load to decide
whether to show the login page. When `DASHBOARD_TOKEN` is set:

- `/dashboard/login.html` accepts the token and stores it both as a cookie
  (path-scoped to `/dashboard`, so static asset GETs carry it) and in
  `localStorage` (so `app.js` adds it as `X-Dashboard-Token` on `/api/*` fetches)
- `/dashboard/vendor/*` and `/dashboard/login.html` are always reachable so the
  login page can load before the user has a token

When `DASHBOARD_TOKEN` is empty (the default), all `/dashboard/*` and
`/api/dashboard/*` endpoints are public. The dashboard shows a yellow warning
banner so an open-source operator running on a LAN doesn't accidentally expose
feed URLs and dead-article error messages.
