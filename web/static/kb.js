/* kbModal() — Alpine 3 component for the per-intent KB (events index) editor.
 *
 * Decoupled from intentsTab: an intent row dispatches `kb-open`
 * ({intentId, intentName}); this component (listening @kb-open.window) loads and
 * shows a modal with the events.md editor + Rebuild / Lint / Open-in-new-window.
 * The same component also drives the standalone full-screen page (kb.html) via
 * initStandalone(), which reads ?intent=&name= from the URL.
 *
 * Consumes: GET/PUT /api/kb/{id}/{kind}, POST /api/kb/{id}/rebuild|lint.
 * Auth: X-Dashboard-Token header (cookie path doesn't cover /api/).
 * Design: sembr-dev-docs/development/delta-label-accuracy/kb/design.md §6.
 */

function kbModal() {
  return {
    open_: false,
    standalone: false,
    intentId: null,
    intentName: '',
    kind: 'events',
    exists: false,

    content: '',
    originalContent: '',
    baseHash: null,

    loading: false,
    submitting: false,
    rebuilding: false,
    linting: false,
    error: '',
    status: '',
    warnings: [],

    // kb_enabled: daily cron auto-ingest switch (independent of the editor).
    enabled: false,
    toggleBusy: false,

    // Days of history the cold-start distill (Build/Rebuild) reads. KB is a
    // long-term index, so default larger than the daily history_days.
    rebuildDays: 60,
    rebuildDialog: false,  // the Build/Rebuild confirm dialog (days input + overwrite warning)

    view: 'edit',          // modal tab: 'edit' (highlighted source) | 'preview' (rendered)
    _syncing: false,       // re-entrancy guard for the split-view proportional scroll sync

    // Generation guard: bumped on every (re)load so a slow in-flight GET for a
    // previously-opened intent can't overwrite the content of a later one
    // (memory feedback_alpine_modal_async_guard / feedback_frontend in-flight guard).
    _gen: 0,

    get dirty() {
      return this.content !== this.originalContent;
    },

    // Display-only markdown syntax highlight for the overlay editor (codehl.js).
    highlightMarkdown(text) {
      return window.ceHighlightMarkdown ? window.ceHighlightMarkdown(text) + '\n' : text;
    },

    // Rendered (sanitized) markdown for the preview pane (marked + DOMPurify).
    get renderedHtml() {
      if (window.marked && window.DOMPurify) {
        return window.DOMPurify.sanitize(
          window.marked.parse(this.content || '', { breaks: true, gfm: true }));
      }
      return '<p class="muted">(markdown renderer unavailable)</p>';
    },

    // Proportional scroll sync for the split view: map src scroll % → dst.
    syncScroll(srcEl, dstEl) {
      if (this._syncing || !srcEl || !dstEl) return;
      this._syncing = true;
      const sMax = Math.max(1, srcEl.scrollHeight - srcEl.clientHeight);
      const dMax = Math.max(0, dstEl.scrollHeight - dstEl.clientHeight);
      dstEl.scrollTop = (srcEl.scrollTop / sMax) * dMax;
      requestAnimationFrame(() => { this._syncing = false; });
    },

    // ── HTTP helpers (mirror templates.js) ─────────────────
    _token() {
      try { return localStorage.getItem('sembr_dashboard_token') || ''; }
      catch (_) { return ''; }
    },

    _extractError(data, fallback) {
      if (typeof data?.detail === 'string') return data.detail;
      if (data?.detail && typeof data.detail === 'object' && data.detail.reason)
        return data.detail.reason;
      if (Array.isArray(data?.detail) && data.detail.length)
        return data.detail.map(e => {
          const loc = (e.loc || []).slice(-1)[0];
          return loc ? `${loc}: ${e.msg}` : e.msg;
        }).join('; ');
      return data?.error || String(fallback);
    },

    async _request(method, path, body) {
      const headers = {};
      const t = this._token();
      if (t) headers['X-Dashboard-Token'] = t;
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const opts = { method, headers };
      if (body !== undefined) opts.body = JSON.stringify(body);
      const res = await fetch(path, opts);
      if (res.status === 401) {
        window.location.href = '/dashboard/login.html';
        throw new Error('unauthorized');
      }
      return res;
    },

    // ── Open / close ───────────────────────────────────────
    open(detail) {
      this.intentId = detail.intentId;
      this.intentName = detail.intentName || ('intent ' + detail.intentId);
      this.kind = 'events';
      this.open_ = true;
      this.error = '';
      this.status = '';
      this.warnings = [];
      this.load();
    },

    close() {
      if (this.dirty && !confirm('Discard unsaved changes?')) return;
      this.open_ = false;
    },

    initStandalone() {
      const p = new URLSearchParams(window.location.search);
      this.standalone = true;
      this.open_ = true;
      this.intentId = p.get('intent');
      this.intentName = p.get('name') || ('intent ' + this.intentId);
      this.kind = 'events';
      this.load();
    },

    // ── Data ───────────────────────────────────────────────
    async load() {
      if (this.intentId == null) return;
      const gen = ++this._gen;
      this.loading = true;
      this.error = '';
      try {
        const res = await this._request('GET', `/api/kb/${this.intentId}/${this.kind}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const d = await res.json();
        if (gen !== this._gen) return;  // a newer load superseded this one
        this.content = d.content || '';
        this.originalContent = this.content;
        this.baseHash = d.content_hash;
        this.exists = d.exists;
        this.enabled = !!d.kb_enabled;
        this.warnings = [];
      } catch (e) {
        if (gen === this._gen) this.error = 'Load failed: ' + e.message;
      } finally {
        if (gen === this._gen) this.loading = false;
      }
    },

    async save() {
      if (this.submitting || !this.dirty) return;
      this.submitting = true;
      this.error = '';
      this.status = '';
      try {
        const res = await this._request(
          'PUT', `/api/kb/${this.intentId}/${this.kind}`,
          { content: this.content, base_hash: this.baseHash },
        );
        const d = await res.json().catch(() => ({}));
        if (res.status === 409) {
          this.error = 'KB changed since you loaded it — reload to get the latest, then re-apply your edit.';
          return;
        }
        if (!res.ok) {
          this.error = this._extractError(d, `HTTP ${res.status}`);
          return;
        }
        this.originalContent = this.content;
        this.baseHash = d.content_hash;
        this.exists = true;
        this.warnings = d.warnings || [];
        this.status = this.warnings.length
          ? `Saved with ${this.warnings.length} warning(s)`
          : 'Saved';
      } catch (e) {
        this.error = 'Network error: ' + e.message;
      } finally {
        this.submitting = false;
      }
    },

    async rebuild() {
      // Confirmation + overwrite warning live in the rebuild dialog (O3); close it.
      this.rebuildDialog = false;
      this.rebuilding = true;
      this.error = '';
      this.status = `Rebuilding from the last ${this.rebuildDays} days… this can take a while.`;
      try {
        const res = await this._request(
          'POST', `/api/kb/${this.intentId}/rebuild`,
          { confirm: true, days: Number(this.rebuildDays) || undefined },
        );
        const d = await res.json().catch(() => ({}));
        if (!res.ok) {
          this.error = this._extractError(d, `HTTP ${res.status}`);
          this.status = '';
          return;
        }
        this.status = `Rebuilt from ${d.days} days: ${d.events} events`;
        await this.load();
      } catch (e) {
        this.error = 'Network error: ' + e.message;
        this.status = '';
      } finally {
        this.rebuilding = false;
      }
    },

    async lint() {
      this.linting = true;
      this.error = '';
      try {
        const res = await this._request('POST', `/api/kb/${this.intentId}/lint`);
        const d = await res.json().catch(() => ({}));
        if (!res.ok) {
          this.error = this._extractError(d, `HTTP ${res.status}`);
          return;
        }
        this.status = `Lint: ${d.merged_dups} dup-merged · ${d.merged_near_dup} near-dup-merged · `
          + `${d.archived} archived · ${d.marked} marked`;
        await this.load();
      } catch (e) {
        this.error = 'Network error: ' + e.message;
      } finally {
        this.linting = false;
      }
    },

    async toggleKbEnabled(checked) {
      // Daily cron auto-ingest switch. Optimistic; reverts on failure.
      const gen = this._gen;
      this.toggleBusy = true;
      this.error = '';
      try {
        const res = await this._request('PUT', `/intents/${this.intentId}`, { kb_enabled: checked });
        const d = await res.json().catch(() => ({}));
        if (gen !== this._gen) return;
        if (!res.ok) {
          this.error = this._extractError(d, `HTTP ${res.status}`);
          this.enabled = !checked;  // revert the switch
          return;
        }
        this.enabled = !!d.kb_enabled;
        this.status = this.enabled ? 'Auto-update enabled' : 'Auto-update disabled';
      } catch (e) {
        if (gen === this._gen) { this.error = 'Network error: ' + e.message; this.enabled = !checked; }
      } finally {
        if (gen === this._gen) this.toggleBusy = false;
      }
    },

    openNewWindow() {
      const url = `/dashboard/kb.html?intent=${this.intentId}`
        + `&name=${encodeURIComponent(this.intentName)}`;
      window.open(url, '_blank', 'noopener');
    },
  };
}
