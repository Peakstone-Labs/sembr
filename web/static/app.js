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

function dashboard() {
  return {
    snapshot: {},
    pollInterval: 10000,
    authRequired: false,
    lastUpdated: '',
    drawer: { kind: null, title: '', rows: [], detail: null, loading: false },
    _embedChart: null,
    _timer: null,
    _refreshing: false,

    // Tab routing
    currentTab: 'dashboard',

    async init() {
      this._syncFromHash();
      window.addEventListener('hashchange', () => this._syncFromHash());

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
      if (hash.startsWith('intents'))      this.currentTab = 'intents';
      else if (hash.startsWith('logs'))    this.currentTab = 'logs';
      else if (hash.startsWith('feeds'))   this.currentTab = 'feeds';
      else if (hash.startsWith('settings')) this.currentTab = 'settings';
      else                                 this.currentTab = 'dashboard';
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

    async _api(path) {
      const headers = {};
      const t = this._token();
      if (t) headers['X-Dashboard-Token'] = t;
      const res = await fetch(path, { headers });
      if (res.status === 401) {
        // Token expired or wrong — bounce to login.
        window.location.href = '/dashboard/login.html';
        throw new Error('unauthorized');
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    },

    async refresh() {
      if (this._refreshing) return;
      this._refreshing = true;
      try {
        this.snapshot = await this._api('/api/dashboard/snapshot');
        this.lastUpdated = new Date().toLocaleTimeString();
        this._renderEmbedChart();
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

    closeDrawer() {
      this.drawer = { kind: null, title: '', rows: [], detail: null, loading: false };
    },

    async openFeedEvents(feedId, feedName) {
      this.drawer = { kind: 'feed-events', title: `events · ${feedName}`,
                      rows: [], detail: null, loading: true };
      try {
        this.drawer.rows = await this._api(`/api/dashboard/feeds/${feedId}/events?limit=100`);
      } finally { this.drawer.loading = false; }
    },

    async openEmbedderEvents() {
      this.drawer = { kind: 'embedder-events', title: 'embedder · recent calls',
                      rows: [], detail: null, loading: true };
      try {
        this.drawer.rows = await this._api(`/api/dashboard/embedder/events?limit=100`);
      } finally { this.drawer.loading = false; }
    },

    async openArticles(bucket) {
      this.drawer = { kind: 'articles', title: `articles · ${bucket}`,
                      rows: [], detail: null, loading: true };
      try {
        this.drawer.rows = await this._api(
          `/api/dashboard/articles?bucket=${bucket}&limit=50`
        );
      } finally { this.drawer.loading = false; }
    },

    async openArticleDetail(md5, bucket) {
      this.drawer.detail = null;
      try {
        this.drawer.detail = await this._api(
          `/api/dashboard/articles/${md5}?bucket=${bucket}`
        );
      } catch (e) { console.error(e); }
    },

    logout() {
      try { localStorage.removeItem('sembr_dashboard_token'); } catch (e) {}
      document.cookie =
        'sembr_dashboard_token=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';
      window.location.href = '/dashboard/login.html';
    },
  };
}
