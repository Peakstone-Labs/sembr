/* Alpine.js dashboard component + Chart.js sparkline.
 * Polls /api/dashboard/snapshot at the cadence reported by /config.
 * Token (if any) is read from localStorage and sent as X-Dashboard-Token.
 */

function dashboard() {
  return {
    snapshot: {},
    pollInterval: 10000,
    authRequired: false,
    lastUpdated: '',
    drawer: { kind: null, title: '', rows: [], detail: null, loading: false },
    _embedChart: null,
    _timer: null,

    async init() {
      try {
        const cfgRes = await fetch('/api/dashboard/config');
        if (cfgRes.ok) {
          const cfg = await cfgRes.json();
          this.pollInterval = (cfg.poll_interval_seconds || 10) * 1000;
          this.authRequired = cfg.auth_required;
        }
      } catch (e) {}
      await this.refresh();
      this._timer = setInterval(() => this.refresh(), this.pollInterval);
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
      try {
        this.snapshot = await this._api('/api/dashboard/snapshot');
        this.lastUpdated = new Date().toLocaleTimeString();
        this._renderEmbedChart();
      } catch (e) {
        console.error('refresh failed', e);
      }
    },

    _renderEmbedChart() {
      const data = this.snapshot.embedder?.calls_24h?.sparkline_latency_ms || [];
      const ctx = document.getElementById('embed-chart');
      if (!ctx) return;
      if (this._embedChart) {
        this._embedChart.data.datasets[0].data = data;
        this._embedChart.update('none');
        return;
      }
      this._embedChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: data.map((_, i) => `${24 - i}h`),
          datasets: [{
            label: 'avg ms',
            data,
            backgroundColor: '#4c7da6',
          }],
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { display: false },
            y: { beginAtZero: true, ticks: { precision: 0 } },
          },
        },
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
        'sembr_dashboard_token=; path=/dashboard; expires=Thu, 01 Jan 1970 00:00:00 GMT';
      window.location.href = '/dashboard/login.html';
    },
  };
}
