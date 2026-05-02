/* Alpine.js logsTab() component — dashboard Logs tab.
 * Connects to GET /api/dashboard/logs/stream?tag=<tag> via EventSource (SSE).
 * Cookie auth is used automatically (no custom header, EventSource limitation).
 * Sub-tab state mirrors the Intents tab pattern: subTab / setSubTab().
 */

const _LOG_TAGS = ['collector', 'embedder', 'matcher', 'notifier', 'api', 'scheduler', 'http'];
const _LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];
const _MAX_ROWS = 1000;

function logsTab() {
  return {
    subTab: 'scheduler',
    tagLevels: Object.fromEntries(_LOG_TAGS.map(t => [t, 'INFO'])),
    rows: [],          // displayed rows (current subTab)
    _rowsMap: {},      // tag → row array  (ring buffer per tag, maxlen _MAX_ROWS)
    _es: null,         // active EventSource
    _seenIds: new Set(), // dedup by ts+logger+message key
    loading: true,
    error: null,

    init() {
      for (const tag of _LOG_TAGS) this._rowsMap[tag] = [];
      this._loadLevels();
      this._connect(this.subTab);
    },

    setSubTab(tag) {
      if (this.subTab === tag) return;
      this.subTab = tag;
      this.rows = this._rowsMap[tag] || [];
      this._reconnect(tag);
    },

    // ── SSE connection ──────────────────────────────────────────────────────

    _reconnect(tag) {
      if (this._es) {
        this._es.close();
        this._es = null;
      }
      this.loading = true;
      this.error = null;
      this._connect(tag);
    },

    _connect(tag) {
      try {
        const url = `/api/dashboard/logs/stream?tag=${encodeURIComponent(tag)}`;
        const es = new EventSource(url, { withCredentials: true });
        this._es = es;

        es.addEventListener('log', (e) => {
          try {
            const entry = JSON.parse(e.data);
            const key = `${entry.ts}|${entry.logger}|${entry.message}`;
            if (this._seenIds.has(key)) return;
            this._seenIds.add(key);
            const arr = this._rowsMap[entry.tag];
            if (!arr) return;
            arr.push(entry);
            if (arr.length > _MAX_ROWS) arr.splice(0, arr.length - _MAX_ROWS);
            if (entry.tag === this.subTab) {
              this.rows = arr.slice();
            }
          } catch (err) {
            console.warn('[logs] parse error', err);
          }
        });

        es.addEventListener('history-end', () => {
          this.loading = false;
          this.rows = (this._rowsMap[this.subTab] || []).slice();
        });

        es.onerror = () => {
          this.loading = false;
          if (es.readyState === EventSource.CLOSED) {
            this.error = 'Stream disconnected. Reconnecting…';
            // EventSource auto-reconnects; clear error on next open
          }
        };

        es.onopen = () => {
          this.error = null;
        };
      } catch (e) {
        this.loading = false;
        this.error = String(e);
      }
    },

    // ── Level management ────────────────────────────────────────────────────

    async _loadLevels() {
      try {
        const headers = {};
        const t = this._token();
        if (t) headers['X-Dashboard-Token'] = t;
        const res = await fetch('/api/dashboard/logs/tags', { headers });
        if (!res.ok) return;
        const data = await res.json();
        for (const tag of (data.tags || [])) {
          this.tagLevels[tag.name] = _levelName(tag.level);
        }
      } catch (e) {
        console.warn('[logs] failed to load tag levels', e);
      }
    },

    async setLevel(tag, levelName) {
      this.tagLevels[tag] = levelName;
      try {
        const headers = { 'Content-Type': 'application/json' };
        const t = this._token();
        if (t) headers['X-Dashboard-Token'] = t;
        await fetch('/api/dashboard/logs/level', {
          method: 'PUT',
          headers,
          body: JSON.stringify({ tag, level: levelName }),
        });
      } catch (e) {
        console.warn('[logs] setLevel failed', e);
      }
    },

    // ── Display helpers ─────────────────────────────────────────────────────

    fmtTs(tsMs) {
      const d = new Date(tsMs);
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      const ms = String(d.getMilliseconds()).padStart(3, '0');
      return `${hh}:${mm}:${ss}.${ms}`;
    },

    levelClass(level) {
      if (level === 'DEBUG')   return 'log-level-debug';
      if (level === 'WARNING') return 'log-level-warning';
      if (level === 'ERROR')   return 'log-level-error';
      return 'log-level-info';
    },

    allTags() { return _LOG_TAGS; },
    allLevels() { return _LOG_LEVELS; },

    _token() {
      try { return localStorage.getItem('sembr_dashboard_token') || ''; }
      catch (e) { return ''; }
    },

    // Clean up on Alpine destroy (subtab switch handled by _reconnect)
    destroy() {
      if (this._es) { this._es.close(); this._es = null; }
    },
  };
}

function _levelName(levelNo) {
  if (levelNo <= 10) return 'DEBUG';
  if (levelNo <= 20) return 'INFO';
  if (levelNo <= 30) return 'WARNING';
  return 'ERROR';
}
