/* Alpine.js Feeds tab component.
 *
 * Endpoints used:
 *   GET    /api/dashboard/feeds              paged list with tags + group_key
 *   GET    /api/dashboard/feeds/{id}/events  drill-down: feed_fetch_log
 *   GET    /api/dashboard/feeds/{id}/articles drill-down: Qdrant articles for this feed (D2)
 *   GET    /api/dashboard/sources/schemas    JSON-Schema map for source_type → form fields
 *   GET    /api/dashboard/articles/{md5}?bucket=qdrant  full body modal
 *   POST   /feeds                            create (with tags)
 *   PATCH  /feeds/{id}/tags                  replace tag set
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
        this.toast(`invalid tag: ${raw}`, 'err');
        return;
      }
      if (this.expanded.tagEdit.tags.length >= 10) {
        this.toast('max 10 tags', 'err'); return;
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
        this.toast('tags saved', 'ok');
      } catch (e) {
        this.toast(`save failed: ${e.message}`, 'err');
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
    async submitCreate() {
      this.create.errors = {};
      if (!this.create.form.name.trim()) {
        this.create.errors.name = 'required'; return;
      }
      if (!/^https?:\/\//i.test(this.create.form.url.trim())) {
        this.create.errors.url = 'must start with http:// or https://'; return;
      }
      this.create.submitting = true;
      const body = {
        name: this.create.form.name.trim(),
        url: this.create.form.url.trim(),
        source_type: this.create.form.source_type,
        poll_interval_minutes: parseInt(this.create.form.poll_interval_minutes, 10) || 30,
        config: this.create.form.config || {},
        tags: this.create.form.tags,
      };
      try {
        await this._api('/feeds', { method: 'POST', body: JSON.stringify(body) });
        this.create.open = false;
        this.toast('feed created', 'ok');
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
        this.toast('feed deleted', 'ok');
        if (this.expandedFeedId === feed.id) this.collapseRow();
        await this.refresh();
      } catch (e) {
        this.toast(`delete failed: ${e.message}`, 'err');
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
