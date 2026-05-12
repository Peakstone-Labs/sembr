/* Alpine.js Feeds tab component.
 *
 * Endpoints used:
 *   GET    /api/dashboard/feeds              paged list with tags + group_key
 *   GET    /api/dashboard/feeds/{id}/events  drill-down: feed_fetch_log
 *   GET    /api/dashboard/feeds/{id}/articles drill-down: Qdrant articles for this feed (D2)
 *   GET    /api/dashboard/sources/schemas    JSON-Schema map for source_type → form fields
 *   GET    /api/dashboard/articles/{md5}?bucket=qdrant  full body modal
 *   POST   /feeds                            create (with tags)
 *   PATCH  /feeds/{id}                      edit name/tags/poll_interval/config/enabled
 *   PATCH  /feeds/{id}/tags                  replace tag set (legacy path, kept)
 *   POST   /feeds/{id}/fire?dry_run=bool     fire feed (returns task_id)
 *   GET    /feeds/{id}/fire/{task_id}        poll fire task status
 *   DELETE /feeds/{id}                       delete (cascade handled server-side)
 */

function feedsTab() {
  return {
    // listing state
    items: [],
    total: 0,
    page: 0,                 // 0-indexed
    pageSize: 20,
    filterTag: '',
    filterQ: '',
    loading: false,
    error: '',

    // single-row inline expansion (D7)
    expandedFeedId: null,
    expandedPageSize: 10,
    expanded: { kind: null, rows: [], loading: false, articleDetail: null,
                tagEdit: null, page: 0, hasMore: false, feed: null },

    // create modal
    schemas: {},             // {source_type: json-schema}
    recommendedSources: [],  // [{uri, title, paywalled}, ...] — datalist for newsapi (D14/D15)
    create: {
      open: false, submitting: false,
      form: { name: '', url: '', source_type: 'rss',
              poll_interval_minutes: 30, tags: [], tagInput: '', config: {} },
      errors: {},
    },

    // delete confirm
    del: { open: false, feed: null },

    async init() {
      // Schemas first so the create modal can render the moment it opens.
      try {
        const res = await this._api('/api/dashboard/sources/schemas');
        this.schemas = res.schemas || {};
      } catch (e) { /* render with empty schema map; create form falls back */ }
      // Pre-fetch the newsapi datalist once so the combobox is populated when
      // the modal opens. Failure is non-fatal — datalist just stays empty and
      // user can still type a hostname manually.
      try {
        const list = await this._api('/api/dashboard/sources/newsapi/recommended_sources');
        this.recommendedSources = Array.isArray(list) ? list : [];
      } catch (e) { this.recommendedSources = []; }
      await this.refresh();
    },

    // ── HTTP helpers (mirror app.js token + 401 handling) ──
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

    // ── List + paging ──
    async refresh() {
      this.loading = true;
      this.error = '';
      try {
        const params = new URLSearchParams({
          limit: String(this.pageSize),
          offset: String(this.page * this.pageSize),
        });
        if (this.filterTag) params.set('tag', this.filterTag.trim().toLowerCase());
        if (this.filterQ)   params.set('q',   this.filterQ.trim());
        const data = await this._api('/api/dashboard/feeds?' + params.toString());
        this.items = data.items || [];
        this.total = data.total || 0;
      } catch (e) {
        this.error = String(e.message || e);
      } finally { this.loading = false; }
    },

    nextPage() {
      if ((this.page + 1) * this.pageSize < this.total) {
        this.page += 1; this.collapseRow(); this.refresh();
      }
    },
    prevPage() {
      if (this.page > 0) { this.page -= 1; this.collapseRow(); this.refresh(); }
    },
    onFilterChange() { this.page = 0; this.collapseRow(); this.refresh(); },

    // ── Inline expand (D7) ──
    async toggleRow(feed, kind) {
      // Same row + same kind → collapse. Same row + different kind → switch view.
      // Different row → switch row + load.
      if (this.expandedFeedId === feed.id && this.expanded.kind === kind) {
        this.collapseRow();
        return;
      }
      this.expandedFeedId = feed.id;
      this.expanded = { kind, rows: [], loading: true, articleDetail: null,
                        tagEdit: null, page: 0, hasMore: false, feed };
      await this._loadExpanded();
    },
    async _loadExpanded() {
      const f = this.expanded.feed;
      if (!f) return;
      this.expanded.loading = true;
      const lim = this.expandedPageSize;
      const off = this.expanded.page * lim;
      const path = this.expanded.kind === 'events'
        ? `/api/dashboard/feeds/${f.id}/events?limit=${lim}&offset=${off}`
        : `/api/dashboard/feeds/${f.id}/articles?limit=${lim}&offset=${off}`;
      try {
        const rows = await this._api(path) || [];
        this.expanded.rows = rows;
        this.expanded.hasMore = rows.length === lim;
      } catch (e) {
        this.expanded.rows = [];
        this.expanded.error = String(e.message || e);
        this.expanded.hasMore = false;
      } finally { this.expanded.loading = false; }
    },
    expandedNextPage() {
      if (!this.expanded.hasMore) return;
      this.expanded.page += 1;
      this._loadExpanded();
    },
    expandedPrevPage() {
      if (this.expanded.page === 0) return;
      this.expanded.page -= 1;
      this._loadExpanded();
    },
    collapseRow() {
      this.expandedFeedId = null;
      this.expanded = { kind: null, rows: [], loading: false, articleDetail: null,
                        tagEdit: null, page: 0, hasMore: false, feed: null };
    },
    truncateBody(s, n) {
      if (!s) return '';
      return s.length > n ? s.substring(0, n) + '…' : s;
    },
    async openArticleBody(md5) {
      this.expanded.articleDetail = { loading: true };
      try {
        const detail = await this._api(`/api/dashboard/articles/${md5}?bucket=qdrant`);
        this.expanded.articleDetail = Object.assign({ loading: false }, detail || {});
      } catch (e) {
        this.expanded.articleDetail = { loading: false, error: String(e.message || e) };
      }
    },
    closeArticleBody() { this.expanded.articleDetail = null; },

    // ── Tag editing in expanded row ──
    beginTagEdit(feed) {
      this.expanded.tagEdit = { tags: [...(feed.tags || [])], input: '' };
    },
    cancelTagEdit() { this.expanded.tagEdit = null; },
    addTagEdit() {
      const raw = (this.expanded.tagEdit?.input || '').trim().toLowerCase();
      if (!raw) return;
      if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(raw)) {
        this.toast(`Invalid tag: ${raw}`, 'err');
        return;
      }
      if (this.expanded.tagEdit.tags.length >= 10) {
        this.toast('Max 10 tags', 'err'); return;
      }
      if (!this.expanded.tagEdit.tags.includes(raw)) {
        this.expanded.tagEdit.tags.push(raw);
      }
      this.expanded.tagEdit.input = '';
    },
    removeTagEdit(idx) { this.expanded.tagEdit.tags.splice(idx, 1); },
    async saveTagEdit(feed) {
      const tags = this.expanded.tagEdit?.tags || [];
      try {
        const updated = await this._api(`/feeds/${feed.id}/tags`, {
          method: 'PATCH', body: JSON.stringify({ tags }),
        });
        // patch the in-place row so we don't have to reload the page.
        feed.tags = updated.tags || [];
        this.expanded.tagEdit = null;
        this.toast('Tags saved', 'ok');
      } catch (e) {
        this.toast(`Save failed: ${e.message}`, 'err');
      }
    },

    // ── Create modal ──
    openCreate() {
      const types = Object.keys(this.schemas);
      const initialType = types.includes('rss') ? 'rss' : (types[0] || 'rss');
      this.create = {
        open: true, submitting: false, errors: {},
        form: {
          name: '', url: '',
          source_type: initialType,
          poll_interval_minutes: 30,
          tags: [], tagInput: '',
          config: this._defaultsFromSchema(this.schemas[initialType]),
        },
      };
    },
    closeCreate() { this.create.open = false; },
    onSourceTypeChange() {
      this.create.form.config = this._defaultsFromSchema(this.schemas[this.create.form.source_type]);
    },
    _defaultsFromSchema(schema) {
      const out = {};
      if (!schema || schema.type !== 'object' || !schema.properties) return out;
      for (const [key, prop] of Object.entries(schema.properties)) {
        if (prop && Object.prototype.hasOwnProperty.call(prop, 'default')) {
          out[key] = prop.default;
        }
      }
      return out;
    },
    schemaFields(sourceType) {
      // Returns [{name, type, default, description, enum}] for the renderer.
      const schema = this.schemas[sourceType];
      if (!schema || schema.type !== 'object' || !schema.properties) return [];
      const out = [];
      for (const [name, prop] of Object.entries(schema.properties)) {
        const t = prop?.type;
        if (t && !['string', 'integer', 'number', 'boolean'].includes(t)) {
          // Renderer subset (D6); fall through as raw JSON textarea.
          out.push({ name, type: 'raw', description: prop.description || '',
                     default: prop.default });
          continue;
        }
        out.push({
          name, type: t || 'string',
          default: prop.default,
          description: prop.description || '',
          enum: prop.enum || null,
        });
      }
      return out;
    },
    addCreateTag() {
      const raw = (this.create.form.tagInput || '').trim().toLowerCase();
      if (!raw) return;
      if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(raw)) {
        this.create.errors.tags = `invalid tag: ${raw}`;
        return;
      }
      if (this.create.form.tags.length >= 10) {
        this.create.errors.tags = 'max 10 tags'; return;
      }
      if (!this.create.form.tags.includes(raw)) this.create.form.tags.push(raw);
      this.create.form.tagInput = '';
      delete this.create.errors.tags;
    },
    removeCreateTag(idx) { this.create.form.tags.splice(idx, 1); },
    handleCreateTagKey(e) {
      if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); this.addCreateTag(); }
    },
    // D16: client-side mirror of sembr/collector/newsapi.normalize_source_uri.
    // Sent in submitCreate() so the UNIQUE conflict on feeds.url surfaces
    // before round-tripping to the backend; backend re-normalizes anyway.
    normalizeSourceUri(s) {
      let out = (s || '').trim().toLowerCase();
      if (out.startsWith('https://')) out = out.slice(8);
      else if (out.startsWith('http://')) out = out.slice(7);
      if (out.startsWith('www.')) out = out.slice(4);
      return out.replace(/\/+$/, '');
    },
    async submitCreate() {
      this.create.errors = {};
      if (!this.create.form.name.trim()) {
        this.create.errors.name = 'Required'; return;
      }
      const sourceType = this.create.form.source_type;
      let url = this.create.form.url.trim();
      if (sourceType === 'newsapi') {
        // D11/D16: hostname-only. Normalize first, then validate.
        // Mirror of sembr/models.py:_NEWSAPI_HOSTNAME_RE — TLD must contain
        // an alphabetic char so bare IPs (127.0.0.1) reject client-side
        // instead of round-tripping to a backend 422 (review-loop2 🟡-1).
        url = this.normalizeSourceUri(url);
        if (!/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z][a-z0-9-]*[a-z0-9]?$/.test(url)) {
          this.create.errors.url = 'must be a hostname like reuters.com';
          return;
        }
      } else if (!/^https?:\/\//i.test(url)) {
        this.create.errors.url = 'must start with http:// or https://'; return;
      }
      this.create.submitting = true;
      const body = {
        name: this.create.form.name.trim(),
        url,
        source_type: sourceType,
        poll_interval_minutes: parseInt(this.create.form.poll_interval_minutes, 10) || 30,
        config: this.create.form.config || {},
        tags: this.create.form.tags,
      };
      try {
        await this._api('/feeds', { method: 'POST', body: JSON.stringify(body) });
        this.create.open = false;
        this.toast('Feed created', 'ok');
        this.page = 0;
        await this.refresh();
      } catch (e) {
        this.create.errors._global = String(e.message || e);
      } finally { this.create.submitting = false; }
    },

    // ── Delete ──
    confirmDelete(feed) { this.del = { open: true, feed }; },
    closeDelete() { this.del = { open: false, feed: null }; },
    async runDelete() {
      const feed = this.del.feed;
      if (!feed) return;
      try {
        await this._api(`/feeds/${feed.id}`, { method: 'DELETE' });
        this.del = { open: false, feed: null };
        this.toast('Feed deleted', 'ok');
        if (this.expandedFeedId === feed.id) this.collapseRow();
        await this.refresh();
      } catch (e) {
        this.toast(`Delete failed: ${e.message}`, 'err');
      }
    },

    // ── Toggle enabled (D16: optimistic update, rollback on error) ──
    async toggleEnabled(feed) {
      const prev = feed.enabled !== false;  // treat undefined as true (matches :checked binding)
      feed.enabled = !prev;  // optimistic
      try {
        const updated = await this._api(`/feeds/${feed.id}`, {
          method: 'PATCH',
          body: JSON.stringify({ enabled: !prev }),
        });
        feed.enabled = updated.enabled;
        this.toast(feed.enabled ? 'Feed enabled' : 'Feed disabled', 'ok');
      } catch (e) {
        feed.enabled = prev;  // rollback
        this.toast(`Toggle failed: ${e.message}`, 'err');
      }
    },

    // ── Edit modal (D17) ──
    edit: {
      open: false, submitting: false, feed: null,
      form: { name: '', poll_interval_minutes: 30, tags: [], tagInput: '', config: {} },
      errors: {},
    },
    openEdit(feed) {
      this.edit = {
        open: true, submitting: false, feed,
        form: {
          name: feed.name,
          poll_interval_minutes: feed.poll_interval_minutes,
          tags: [...(feed.tags || [])],
          tagInput: '',
          config: Object.assign({}, feed.config || {}),
        },
        errors: {},
      };
    },
    closeEdit() { this.edit.open = false; },
    addEditTag() {
      const raw = (this.edit.form.tagInput || '').trim().toLowerCase();
      if (!raw) return;
      if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(raw)) {
        this.edit.errors.tags = `invalid tag: ${raw}`; return;
      }
      if (this.edit.form.tags.length >= 10) {
        this.edit.errors.tags = 'max 10 tags'; return;
      }
      if (!this.edit.form.tags.includes(raw)) this.edit.form.tags.push(raw);
      this.edit.form.tagInput = '';
      delete this.edit.errors.tags;
    },
    removeEditTag(idx) { this.edit.form.tags.splice(idx, 1); },
    async submitEdit() {
      this.edit.errors = {};
      const feed = this.edit.feed;
      if (!feed) return;
      if (!this.edit.form.name.trim()) {
        this.edit.errors.name = 'Required'; return;
      }
      const body = {};
      if (this.edit.form.name.trim() !== feed.name)
        body.name = this.edit.form.name.trim();
      if (this.edit.form.poll_interval_minutes !== feed.poll_interval_minutes)
        body.poll_interval_minutes = parseInt(this.edit.form.poll_interval_minutes, 10) || feed.poll_interval_minutes;
      if (JSON.stringify(this.edit.form.tags) !== JSON.stringify(feed.tags || []))
        body.tags = this.edit.form.tags;
      if (JSON.stringify(this.edit.form.config) !== JSON.stringify(feed.config || {}))
        body.config = this.edit.form.config;
      if (Object.keys(body).length === 0) { this.edit.open = false; return; }
      this.edit.submitting = true;
      try {
        const updated = await this._api(`/feeds/${feed.id}`, {
          method: 'PATCH', body: JSON.stringify(body),
        });
        // patch in-place so list updates without full refresh
        Object.assign(feed, {
          name: updated.name,
          poll_interval_minutes: updated.poll_interval_minutes,
          tags: updated.tags,
          config: updated.config,
          enabled: updated.enabled,
        });
        this.edit.open = false;
        this.toast('Feed updated', 'ok');
      } catch (e) {
        this.edit.errors._global = String(e.message || e);
      } finally { this.edit.submitting = false; }
    },

    // ── Fire dialog (D18: form → running → result) ──
    fire: {
      open: false, feed: null, dryRun: true,
      phase: 'form',   // 'form' | 'running' | 'result'
      taskId: null, taskUrl: null,
      articles: [], articlesNew: 0, articlesFetched: 0,
      error: null, pollTimer: null,
    },
    openFire(feed) {
      if (this.fire.pollTimer) { clearInterval(this.fire.pollTimer); }
      this.fire = {
        open: true, feed, dryRun: true,
        phase: 'form',
        taskId: null, taskUrl: null,
        articles: [], articlesNew: 0, articlesFetched: 0,
        error: null, pollTimer: null,
      };
    },
    closeFire() {
      if (this.fire.pollTimer) { clearInterval(this.fire.pollTimer); }
      this.fire.taskId = null;  // sentinel: in-flight poll callbacks become no-ops
      this.fire.open = false;
    },
    async runFire() {
      const feed = this.fire.feed;
      if (!feed) return;
      const myFeed = feed;  // capture for session check after POST await
      this.fire.phase = 'running';
      this.fire.error = null;
      try {
        const dryParam = this.fire.dryRun ? 'true' : 'false';
        const res = await this._api(`/feeds/${feed.id}/fire?dry_run=${dryParam}`, { method: 'POST' });
        if (this.fire.feed !== myFeed || !this.fire.open) return;  // closed/reopened during POST
        const myTaskId = res.task_id;
        this.fire.taskId = myTaskId;
        this.fire.taskUrl = `/feeds/${feed.id}/fire/${myTaskId}`;
        // Poll until done; sentinel guards against stale in-flight closures
        this.fire.pollTimer = setInterval(async () => {
          if (this.fire.taskId !== myTaskId) { clearInterval(this.fire.pollTimer); return; }
          try {
            const t = await this._api(`/feeds/${feed.id}/fire/${myTaskId}`);
            if (this.fire.taskId !== myTaskId) return;
            if (t.status === 'done' || t.status === 'error') {
              clearInterval(this.fire.pollTimer);
              this.fire.pollTimer = null;
              this.fire.articles = t.articles || [];
              this.fire.articlesNew = t.articles_new || 0;
              this.fire.articlesFetched = t.articles_fetched || 0;
              if (t.status === 'error') this.fire.error = t.error || 'unknown error';
              this.fire.phase = 'result';
            }
          } catch (e) {
            if (this.fire.taskId !== myTaskId) return;
            clearInterval(this.fire.pollTimer);
            this.fire.pollTimer = null;
            this.fire.error = String(e.message || e);
            this.fire.phase = 'result';
          }
        }, 2000);
      } catch (e) {
        if (this.fire.feed !== myFeed || !this.fire.open) return;  // closed during POST
        this.fire.error = String(e.message || e);
        this.fire.phase = 'result';
      }
    },

    // ── Toast (lightweight; reuses .toast styles from intents) ──
    toasts: [],
    _toastSeq: 0,
    toast(msg, type = 'ok') {
      const id = ++this._toastSeq;
      this.toasts.push({ id, msg, type });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, 3000);
    },

    // ── Render helpers ──
    fmtNextRun(iso) {
      if (!iso) return '—';
      const d = new Date(iso);
      const delta = Math.floor((d - new Date()) / 1000);
      if (delta < 0) return 'due';
      if (delta < 60) return `${delta}s`;
      if (delta < 3600) return `${Math.floor(delta / 60)}m`;
      return `${Math.floor(delta / 3600)}h`;
    },
    pageInfo() {
      if (this.total === 0) return '0 of 0';
      const a = this.page * this.pageSize + 1;
      const b = Math.min((this.page + 1) * this.pageSize, this.total);
      return `${a}–${b} of ${this.total}`;
    },
  };
}
