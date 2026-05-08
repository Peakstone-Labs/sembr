/* Alpine.js dashboard component + Chart.js sparkline.
 * Polls /api/dashboard/snapshot at the cadence reported by /config.
 * Token (if any) is read from localStorage and sent as X-Dashboard-Token.
 */

// Global date/time formatter, configured from /api/dashboard/config.
// Renders YYYY-MM-DD HH:MM in the configured DISPLAY_TIMEZONE.
// Accepts: ISO 8601 string, Unix epoch seconds (number), null/undefined → '—'.
window.sembrTimezone = 'UTC';
window.fmtDateTime = function (value) {
  if (value === null || value === undefined || value === '' || value === 'never') return '—';
  let d;
  if (typeof value === 'number') {
    // ingested_at_ts is epoch seconds; Date wants ms.
    d = new Date(value * 1000);
  } else {
    // ISO string: handle "...Z", "...+00:00", and naive "YYYY-MM-DDTHH:MM:SS"
    // (assume UTC for naive — sembr's timestamps are stored UTC).
    let s = String(value);
    if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s = s + 'Z';
    d = new Date(s);
  }
  if (isNaN(d.getTime())) return String(value);
  // Intl gives reliable timezone math without pulling a tz library.
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: window.sembrTimezone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const get = (t) => (parts.find(p => p.type === t) || {}).value || '';
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`;
};

// Human-friendly uptime. ContainerMetric.uptime_seconds → "3d 4h" / "12m 8s".
window.fmtUptime = function (seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + (s % 60) + 's';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ' + (m % 60) + 'm';
  const d = Math.floor(h / 24);
  return d + 'd ' + (h % 24) + 'h';
};

// Decimal byte formatter — 1 KB = 1000 bytes (matches `docker stats` columns).
window.fmtBytes = function (bytes) {
  if (bytes === null || bytes === undefined) return '—';
  const n = Number(bytes);
  if (!isFinite(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let v = n;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i += 1; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
};

const _RESTART_LOCK_KEY = 'sembr_restart_in_flight';

function dashboard() {
  return {
    snapshot: {},
    pollInterval: 10000,
    authRequired: false,
    lastUpdated: '',
    drawer: {
      kind: null, title: '', rows: [], detail: null, loading: false,
      bucket: null, page: 0, pageSize: 50,
    },
    filter: {
      ingested_from: '', ingested_to: '',
      feed_id: '', title_q: '',
      feedOptions: [],
    },
    restart: { active: false, message: '', startedAt: 0, elapsedMs: 0,
               timer: null, _inFlight: false },
    restartConfirm: { open: false },
    _embedChart: null,
    _containerCharts: {},  // { 'cpu-spark-<name>': Chart, 'mem-spark-<name>': Chart }
    _timer: null,
    _refreshing: false,

    // Tab routing
    currentTab: 'dashboard',

    async init() {
      this._syncFromHash();
      window.addEventListener('hashchange', () => this._syncFromHash());

      // Cross-tab restart lock (D10): listen for other tabs' state.
      // The 'storage' event does NOT fire on the writer tab, so the writer
      // updates this.restart.active itself (see _setRestartLockShared).
      window.addEventListener('storage', (e) => {
        if (e.key !== _RESTART_LOCK_KEY) return;
        if (e.newValue === '1' && !this.restart.active) {
          // Another tab kicked off a restart — sync state without re-POSTing.
          this._beginRestartUI('restart in progress (other tab)');
        } else if (!e.newValue) {
          // Another tab finished or cleared the lock; nothing to do — our
          // own _pollRestart loop also clears state when /health returns.
        }
      });
      // On load, if another tab already set the lock, reflect it.
      try {
        if (localStorage.getItem(_RESTART_LOCK_KEY) === '1') {
          this._beginRestartUI('restart in progress (other tab)');
        }
      } catch (e) {}

      try {
        const cfgRes = await fetch('/api/dashboard/config');
        if (cfgRes.ok) {
          const cfg = await cfgRes.json();
          this.pollInterval = (cfg.poll_interval_seconds || 10) * 1000;
          this.authRequired = cfg.auth_required;
          if (cfg.display_timezone) window.sembrTimezone = cfg.display_timezone;
        }
      } catch (e) {}
      await this.refresh();
      this._timer = setInterval(() => {
        if (this._refreshing) return;
        this.refresh();
      }, this.pollInterval);
    },

    _syncFromHash() {
      const hash = window.location.hash.slice(1) || 'dashboard';
      if (hash.startsWith('intents'))        this.currentTab = 'intents';
      else if (hash.startsWith('templates')) this.currentTab = 'templates';
      else if (hash.startsWith('logs'))      this.currentTab = 'logs';
      else if (hash.startsWith('feeds'))     this.currentTab = 'feeds';
      else if (hash.startsWith('settings'))  this.currentTab = 'settings';
      else                                   this.currentTab = 'dashboard';
    },

    setTab(tab) {
      this.currentTab = tab;
      if (tab === 'dashboard') {
        window.location.hash = 'dashboard';
      } else if (tab === 'feeds') {
        if (!window.location.hash.startsWith('#feeds')) window.location.hash = 'feeds';
      } else if (tab === 'logs') {
        if (!window.location.hash.startsWith('#logs')) {
          window.location.hash = 'logs/scheduler';
        }
      } else if (tab === 'settings') {
        if (!window.location.hash.startsWith('#settings')) window.location.hash = 'settings';
      } else if (tab === 'templates') {
        if (!window.location.hash.startsWith('#templates')) window.location.hash = 'templates';
      } else {
        if (!window.location.hash.startsWith('#intents')) {
          window.location.hash = 'intents/cron';
        }
      }
    },

    _token() {
      try { return localStorage.getItem('sembr_dashboard_token') || ''; }
      catch (e) { return ''; }
    },

    async _api(path, init) {
      const headers = (init && init.headers) ? { ...init.headers } : {};
      const t = this._token();
      if (t) headers['X-Dashboard-Token'] = t;
      const res = await fetch(path, { ...(init || {}), headers });
      if (res.status === 401) {
        // Token expired or wrong — bounce to login.
        window.location.href = '/dashboard/login.html';
        throw new Error('unauthorized');
      }
      if (!res.ok) {
        const body = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status} ${body}`);
      }
      const ct = res.headers.get('content-type') || '';
      return ct.includes('application/json') ? await res.json() : null;
    },

    async refresh() {
      if (this._refreshing) return;
      this._refreshing = true;
      try {
        this.snapshot = await this._api('/api/dashboard/snapshot');
        this.lastUpdated = new Date().toLocaleTimeString();
        this._renderEmbedChart();
        this._renderContainerSparklines();
      } catch (e) {
        console.error('refresh failed', e);
      } finally {
        this._refreshing = false;
      }
    },

    _renderEmbedChart() {
      const data = this.snapshot.embedder?.calls_24h?.sparkline_latency_ms || [];
      if (this._embedChart) {
        this._embedChart.data.datasets[0].data = data;
        this._embedChart.update('none');
        return;
      }
      // Defer first creation to $nextTick so Alpine has finished DOM updates and
      // Chart.js can measure the container's actual width (avoids 0px bar issue).
      this.$nextTick(() => {
        const ctx = document.getElementById('embed-chart');
        if (!ctx) return;
        this._embedChart = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: data.map((_, i) => `${24 - i}h`),
            datasets: [{
              label: 'avg ms',
              data,
              backgroundColor: 'rgba(59,138,138,0.65)',
              borderColor: 'rgba(59,138,138,0.9)',
              borderWidth: 1,
              borderRadius: 2,
            }],
          },
          options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: {
              x: { display: false },
              y: {
                beginAtZero: true,
                ticks: {
                  precision: 0,
                  color: '#5a6678',
                  font: { family: "'JetBrains Mono', monospace", size: 10 },
                },
                grid: { color: 'rgba(30,45,69,0.8)' },
              },
            },
          },
        });
      });
    },

    _renderContainerSparklines() {
      const containers = this.snapshot.system_metrics?.containers;
      if (!containers || !containers.length) return;
      // Prune stale Chart instances (loop 2 🟡-1): when a container drops
      // out of the rolling window, its row leaves the DOM but the Chart.js
      // instance + its dataset arrays would otherwise leak forever. Destroy
      // any chart whose canvas id is no longer in the live container set.
      const liveIds = new Set();
      for (const c of containers) {
        liveIds.add('cpu-spark-' + c.name);
        liveIds.add('mem-spark-' + c.name);
      }
      for (const id of Object.keys(this._containerCharts)) {
        if (!liveIds.has(id)) {
          try { this._containerCharts[id].destroy(); } catch (e) {}
          delete this._containerCharts[id];
        }
      }
      // $nextTick so the canvases for newly-rendered <tr> rows exist.
      this.$nextTick(() => {
        for (const c of containers) {
          this._renderOneSparkline('cpu-spark-' + c.name, c.cpu_history, 'rgba(160,180,90,0.7)');
          this._renderOneSparkline('mem-spark-' + c.name, c.mem_history, 'rgba(59,138,138,0.7)');
        }
      });
    },

    _renderOneSparkline(canvasId, series, color) {
      const ctx = document.getElementById(canvasId);
      if (!ctx) return;
      // Replace null with NaN so Chart.js draws a gap rather than 0.
      const data = (series || []).map(v => (v === null || v === undefined) ? NaN : v);
      // Loop 2 💡-4: with pointRadius=0 a single data point renders as
      // nothing — show a visible dot until the series has ≥2 real points.
      const realPoints = data.filter(v => !Number.isNaN(v)).length;
      const pointRadius = realPoints < 2 ? 1.5 : 0;
      const existing = this._containerCharts[canvasId];
      if (existing) {
        existing.data.labels = data.map((_, i) => i);
        existing.data.datasets[0].data = data;
        existing.data.datasets[0].pointRadius = pointRadius;
        existing.update('none');
        return;
      }
      this._containerCharts[canvasId] = new Chart(ctx, {
        type: 'line',
        data: {
          labels: data.map((_, i) => i),
          datasets: [{
            data, borderColor: color, borderWidth: 1.2,
            pointRadius, pointBackgroundColor: color,
            tension: 0.25, spanGaps: false,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: { legend: { display: false }, tooltip: { enabled: false } },
          scales: { x: { display: false }, y: { display: false, beginAtZero: true } },
        },
      });
    },

    // ── drawer + filter helpers (D11/D12) ─────────────────────────────────
    closeDrawer() {
      // Fresh-state on close (D11): x-show keeps the DOM mounted so Alpine
      // does NOT reset reactive state automatically — without this reset,
      // re-opening the modal would land on the previous filter / page.
      this.drawer = {
        kind: null, title: '', rows: [], detail: null, loading: false,
        bucket: null, page: 0, pageSize: 50,
      };
      this.filter = {
        ingested_from: '', ingested_to: '',
        feed_id: '', title_q: '',
        feedOptions: this.filter.feedOptions,  // keep cached source list
      };
    },

    async openFeedEvents(feedId, feedName) {
      // Loop 2 🟡-2: route every modal-open through closeDrawer first so D11
      // fresh-state applies symmetrically (filter + page state reset).
      this.closeDrawer();
      this.drawer = {
        kind: 'feed-events', title: `events · ${feedName}`,
        rows: [], detail: null, loading: true,
        bucket: null, page: 0, pageSize: 100,
      };
      try {
        this.drawer.rows = await this._api(`/api/dashboard/feeds/${feedId}/events?limit=100`);
      } finally { this.drawer.loading = false; }
    },

    async openEmbedderEvents() {
      this.closeDrawer();
      this.drawer = {
        kind: 'embedder-events', title: 'embedder · recent calls',
        rows: [], detail: null, loading: true,
        bucket: null, page: 0, pageSize: 100,
      };
      try {
        this.drawer.rows = await this._api(`/api/dashboard/embedder/events?limit=100`);
      } finally { this.drawer.loading = false; }
    },

    async openArticles(bucket) {
      // Symmetric with openFeedEvents/openEmbedderEvents: single-source-of-
      // truth state reset via closeDrawer (loop 2 🟡-2).
      this.closeDrawer();
      this.drawer = {
        kind: 'articles', title: `articles · ${bucket}`,
        rows: [], detail: null, loading: true,
        bucket, page: 0, pageSize: 50,
      };
      try {
        if (bucket === 'qdrant') {
          await this._loadFeedUniverse();
        }
        this.drawer.rows = await this._fetchArticles();
      } finally { this.drawer.loading = false; }
    },

    async _loadFeedUniverse() {
      // D9: source dropdown reuses /api/dashboard/maintenance/feed_universe.
      // alive items have a name; deleted items have name=null → fallback label.
      try {
        const res = await this._api('/api/dashboard/maintenance/feed_universe');
        const opts = [];
        for (const f of (res?.alive || [])) opts.push({ id: f.id, label: `${f.name} (id=${f.id})` });
        for (const f of (res?.deleted || [])) opts.push({ id: f.id, label: `Feed #${f.id} (deleted)` });
        this.filter.feedOptions = opts;
      } catch (e) {
        // Non-fatal: filter still works without the dropdown.
        console.warn('feed_universe load failed:', e);
      }
    },

    async _fetchArticles() {
      const params = new URLSearchParams();
      params.set('bucket', this.drawer.bucket);
      params.set('limit', String(this.drawer.pageSize));
      params.set('offset', String(this.drawer.page * this.drawer.pageSize));
      if (this.drawer.bucket === 'qdrant') {
        if (this.filter.ingested_from) params.set('ingested_from', this.filter.ingested_from);
        if (this.filter.ingested_to)   params.set('ingested_to',   this.filter.ingested_to);
        if (this.filter.feed_id !== '' && this.filter.feed_id !== null && this.filter.feed_id !== undefined) {
          params.set('feed_id', String(this.filter.feed_id));
        }
        if (this.filter.title_q && this.filter.title_q.trim()) {
          params.set('title_q', this.filter.title_q.trim());
        }
      }
      return await this._api('/api/dashboard/articles?' + params.toString());
    },

    async applyFilter() {
      this.drawer.page = 0;
      this.drawer.loading = true;
      try { this.drawer.rows = await this._fetchArticles(); }
      finally { this.drawer.loading = false; }
    },

    async clearFilter() {
      this.filter.ingested_from = '';
      this.filter.ingested_to = '';
      this.filter.feed_id = '';
      this.filter.title_q = '';
      await this.applyFilter();
    },

    async prevPage() {
      if (this.drawer.page === 0) return;
      this.drawer.page -= 1;
      this.drawer.loading = true;
      try { this.drawer.rows = await this._fetchArticles(); }
      finally { this.drawer.loading = false; }
    },

    async nextPage() {
      // No total count — paginate optimistically; "next" is disabled when the
      // last page returned fewer rows than pageSize (template).
      this.drawer.page += 1;
      this.drawer.loading = true;
      try { this.drawer.rows = await this._fetchArticles(); }
      finally { this.drawer.loading = false; }
    },

    async openArticleDetail(md5, bucket) {
      // The body modal is driven by `drawer.detail` truthiness — set null
      // first so the previous detail isn't briefly visible while the new
      // one fetches.
      this.drawer.detail = null;
      try {
        this.drawer.detail = await this._api(
          `/api/dashboard/articles/${md5}?bucket=${bucket}`
        );
      } catch (e) { console.error(e); }
    },

    closeArticleDetail() {
      this.drawer.detail = null;
    },

    // ── restart (D1 + D10 + D13) ─────────────────────────────────────────
    openRestartConfirm() {
      this.restartConfirm.open = true;
    },

    closeRestartConfirm() {
      this.restartConfirm.open = false;
    },

    async confirmRestart() {
      this.restartConfirm.open = false;
      this._setRestartLockShared(true);
      this._beginRestartUI('triggering restart…');
      try {
        const res = await this._api('/api/dashboard/restart', { method: 'POST' });
        if (res && res.rsshub_restart_failed) {
          this.restart.message = `RSSHub failed: ${res.rsshub_error || 'unknown'} — api still restarting`;
        } else {
          this.restart.message = 'restart in progress…';
        }
      } catch (e) {
        // Even on POST failure we keep the overlay until /health recovers
        // (the SIGTERM might already have fired before the response was
        // serialised; better to wait than to reset prematurely).
        this.restart.message = `POST failed: ${e.message}`;
      }
    },

    _beginRestartUI(message) {
      if (this.restart.active) return;
      this.restart.active = true;
      this.restart.startedAt = Date.now();
      this.restart.elapsedMs = 0;
      this.restart.message = message;
      this.restart.timer = setInterval(() => this._pollRestart(), 1000);
    },

    async _pollRestart() {
      if (this.restart._inFlight) return;
      this.restart.elapsedMs = Date.now() - this.restart.startedAt;
      if (this.restart.elapsedMs > 60000) {
        this._endRestartUI();
        console.warn('restart timed out (60s) — check container logs');
        return;
      }
      this.restart._inFlight = true;
      try {
        const res = await fetch('/health', { cache: 'no-store' });
        if (res.status === 200) {
          this._endRestartUI();
          // Re-fetch snapshot once healthy so dashboard repaints with fresh data.
          await this.refresh();
        }
      } catch (e) {
        // expected during the restart window
      } finally {
        this.restart._inFlight = false;
      }
    },

    _endRestartUI() {
      if (this.restart.timer) clearInterval(this.restart.timer);
      this.restart.timer = null;
      this.restart.active = false;
      this._setRestartLockShared(false);
    },

    _setRestartLockShared(active) {
      // localStorage 'storage' event fires only in OTHER tabs; the writer
      // tab must update its own state explicitly — D10 absorbing review #6.
      try {
        if (active) localStorage.setItem(_RESTART_LOCK_KEY, '1');
        else localStorage.removeItem(_RESTART_LOCK_KEY);
      } catch (e) {}
      this.restart.active = active;
    },

    logout() {
      try { localStorage.removeItem('sembr_dashboard_token'); } catch (e) {}
      document.cookie =
        'sembr_dashboard_token=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';
      window.location.href = '/dashboard/login.html';
    },
  };
}
