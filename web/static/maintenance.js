/* Alpine.js Maintenance tab component (reconcile design D5 + D6).
 *
 * Drives the "Manual prune" flow on the Dashboard tab:
 *
 *   GET  /api/dashboard/maintenance/feed_universe              picker data
 *   POST /api/dashboard/maintenance/manual_prune               create planning task
 *   GET  /api/dashboard/maintenance/manual_prune/{task_id}     poll task state
 *   POST /api/dashboard/maintenance/manual_prune/{task_id}/confirm  planned → applying
 *
 * Kept intentionally separate from app.js / feeds.js — Alpine x-data scopes
 * don't share methods, so each panel keeps its own _api helper.
 */

function maintenanceTab() {
  return {
    prune: {
      open: false,
      loading: false,
      error: '',
      target: 'news',
      olderThanDays: 35,
      universe: { alive: [], deleted: [] },
      selected: new Set(),
      taskId: null,
      status: null,
      planSummary: null,
      resultSummary: null,
      _pollHandle: null,
    },

    initMaint() {
      // No autoload — feed_universe only fetched when the user opens the modal.
    },

    _token() {
      try { return localStorage.getItem('sembr_dashboard_token') || ''; }
      catch (e) { return ''; }
    },

    async _api(path, opts = {}) {
      const headers = Object.assign({}, opts.headers || {});
      const t = this._token();
      if (t) headers['X-Dashboard-Token'] = t;
      if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
      const res = await fetch(path, Object.assign({}, opts, { headers }));
      if (res.status === 401) {
        window.location.href = '/dashboard/login.html';
        throw new Error('unauthorized');
      }
      if (res.status === 204) return null;
      const text = await res.text();
      const json = text ? JSON.parse(text) : null;
      if (!res.ok) {
        const msg = (json && (json.detail || json.message)) || `HTTP ${res.status}`;
        throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      }
      return json;
    },

    _resetPrune() {
      if (this.prune._pollHandle) {
        clearTimeout(this.prune._pollHandle);
        this.prune._pollHandle = null;
      }
      this.prune.taskId = null;
      this.prune.status = null;
      this.prune.planSummary = null;
      this.prune.resultSummary = null;
      this.prune.error = '';
    },

    async openPrune() {
      this._resetPrune();
      this.prune.open = true;
      this.prune.loading = true;
      try {
        const res = await this._api('/api/dashboard/maintenance/feed_universe');
        this.prune.universe = {
          alive: res.alive || [],
          deleted: res.deleted || [],
        };
        // Default = everything checked (D6 minimal UX).
        const all = new Set();
        for (const f of this.prune.universe.alive) all.add(f.id);
        for (const f of this.prune.universe.deleted) all.add(f.id);
        this.prune.selected = all;
      } catch (e) {
        this.prune.error = e.message || String(e);
      } finally {
        this.prune.loading = false;
      }
    },

    closePrune() {
      // Cancel = no API call (planning/applying tasks linger 5min server-side
      // before sweep). Just clear local state.
      this._resetPrune();
      this.prune.open = false;
    },

    onTargetChange() {
      // News / Dead radio toggle resets the time window to the matching default.
      this.prune.olderThanDays = (this.prune.target === 'news') ? 35 : 14;
    },

    toggleFeed(id) {
      if (this.prune.selected.has(id)) this.prune.selected.delete(id);
      else this.prune.selected.add(id);
      // Force Alpine reactivity on the Set instance.
      this.prune.selected = new Set(this.prune.selected);
    },

    selectAll() {
      const all = new Set();
      for (const f of this.prune.universe.alive) all.add(f.id);
      for (const f of this.prune.universe.deleted) all.add(f.id);
      this.prune.selected = all;
    },

    selectNone() {
      this.prune.selected = new Set();
    },

    async runDryRun() {
      this.prune.error = '';
      const feed_ids = Array.from(this.prune.selected);
      if (feed_ids.length === 0) {
        this.prune.error = 'select at least one feed';
        return;
      }
      const days = parseInt(this.prune.olderThanDays, 10);
      if (!Number.isFinite(days) || days < 1) {
        this.prune.error = '"older than" must be a positive integer';
        return;
      }
      try {
        const res = await this._api('/api/dashboard/maintenance/manual_prune', {
          method: 'POST',
          body: JSON.stringify({
            target: this.prune.target,
            feed_ids,
            older_than_days: days,
          }),
        });
        this.prune.taskId = res.task_id;
        this.prune.status = 'planning';
        this._schedulePoll();
      } catch (e) {
        this.prune.error = e.message || String(e);
      }
    },

    async confirmDelete() {
      if (!this.prune.taskId) return;
      try {
        await this._api(
          `/api/dashboard/maintenance/manual_prune/${this.prune.taskId}/confirm`,
          { method: 'POST' },
        );
        this.prune.status = 'applying';
        this._schedulePoll();
      } catch (e) {
        this.prune.error = e.message || String(e);
      }
    },

    backToPicker() {
      this._resetPrune();
    },

    _schedulePoll() {
      if (this.prune._pollHandle) return;
      this.prune._pollHandle = setTimeout(() => {
        this.prune._pollHandle = null;
        this._pollOnce();
      }, 500);
    },

    async _pollOnce() {
      if (!this.prune.taskId) return;
      try {
        const t = await this._api(
          `/api/dashboard/maintenance/manual_prune/${this.prune.taskId}`,
        );
        this.prune.status = t.status;
        this.prune.planSummary = t.plan_summary || null;
        this.prune.resultSummary = t.result_summary || null;
        if (t.error) this.prune.error = t.error;
        // Keep polling while in transitional states.
        if (t.status === 'planning' || t.status === 'applying') {
          this._schedulePoll();
        }
      } catch (e) {
        this.prune.error = e.message || String(e);
      }
    },
  };
}
