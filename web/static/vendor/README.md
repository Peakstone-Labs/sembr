# Dashboard Vendor JS

Browser libraries vendored into the repo (not loaded from CDN at runtime).

## Pinned Versions

| Package    | Version | Source                                                                  | Purpose                                                  |
|------------|---------|-------------------------------------------------------------------------|----------------------------------------------------------|
| Alpine.js  | (pre-existing) | bundled                                                          | Dashboard reactivity                                     |
| Chart.js   | (pre-existing) | bundled                                                          | Dashboard sparklines                                     |
| marked     | 15.0.12 | `https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js`             | Render LLM-produced markdown in the History View modal   |
| DOMPurify  | 3.4.6   | `https://cdn.jsdelivr.net/npm/dompurify@3.4.6/dist/purify.min.js`       | Sanitize rendered markdown before innerHTML (XSS guard)  |

## Subresource Integrity (SHA-384, base64)

If you ever load these from CDN (we currently self-host), use these hashes:

```
marked 15.0.12      sha384-948ahk4ZmxYVYOc+rxN1H2gM1EJ2Duhp7uHtZ4WSLkV4Vtx5MUqnV+l7u9B+jFv+
DOMPurify 3.4.6     sha384-ANd4SA3BlDn2fYY4jl9gCPJyNidu+a/JUff2qJreRfrHHTJRXaDtfMyLPB81ymuI
```

Recompute after re-fetching:

```bash
openssl dgst -sha384 -binary web/static/vendor/marked.min.js | openssl base64 -A
```

## Refresh Policy

Re-audit npm advisories (`npm audit` or [snyk.io](https://snyk.io)) for the
two packages every 3 months. When a CVE is published against the pinned
version, refresh via:

```bash
curl -sSL -o web/static/vendor/marked.min.js     https://cdn.jsdelivr.net/npm/marked@<X.Y.Z>/marked.min.js
curl -sSL -o web/static/vendor/dompurify.min.js  https://cdn.jsdelivr.net/npm/dompurify@<X.Y.Z>/dist/purify.min.js
```

Bump `?v=N` cache-buster in `web/static/index.html` so browsers refetch.

## Why not load from CDN directly?

- sembr targets self-hosted deployments behind whatever DNS / firewall the
  user has; CDN reachability isn't guaranteed.
- Vendor-in-repo keeps the dashboard usable offline and removes a runtime
  dependency on jsdelivr's uptime.
- Bundle size is small (marked ~40 KB + DOMPurify ~26 KB ≈ 66 KB total).
