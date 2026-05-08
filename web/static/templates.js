/* templatesTab() — Alpine 3 component for the Templates management view.
 *
 * Consumes: GET / POST / PUT / DELETE / POST-rename on /api/prompts/templates
 * Auth: X-Dashboard-Token header (localStorage); cookie path does not cover /api/.
 * Design: see sembr-dev-docs/development/template-mgmt/design.md (D2/D6/D9/D14/D20)
 */

function templatesTab() {
  return {
    // ── State ──────────────────────────────────────────────
    system: [],          // TemplateInfo[]
    instruction: [],     // TemplateInfo[]
    loading: false,
    _initialized: false,

    editor: {
      open: false,
      kind: null,        // 'system' | 'instruction'
      name: '',
      content: '',
      originalContent: '',
      submitting: false,
      error: '',
    },

    create: {
      open: false,
      kind: null,
      name: '',
      source: '',        // empty string → seed-from-default; non-empty → seed-from-source
      submitting: false,
      error: '',
    },

    rename: {
      open: false,
      kind: null,
      oldName: '',
      newName: '',
      submitting: false,
      error: '',
    },

    del: {
      open: false,
      kind: null,
      name: '',
      refIntents: [],    // populated only when server returns 409
      submitting: false,
      error: '',
    },

    toasts: [],

    // ── Lifecycle ──────────────────────────────────────────
    async init() {
      const maybeLoad = async () => {
        if (window.location.hash.startsWith('#templates') && !this._initialized) {
          this._initialized = true;
          await this.loadList();
        }
      };
      window.addEventListener('hashchange', maybeLoad);
      await maybeLoad();
    },

    // ── HTTP helpers ───────────────────────────────────────
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

    // ── Data loaders ───────────────────────────────────────
    async loadList() {
      this.loading = true;
      try {
        const res = await this._request('GET', '/api/prompts/templates');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this.system      = data.system      || [];
        this.instruction = data.instruction || [];
      } catch (e) {
        this.showToast('Failed to load templates: ' + e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    rowsFor(kind) {
      return kind === 'system' ? this.system : this.instruction;
    },

    // ── Editor (Edit existing template content) ────────────
    async openEditor(row) {
      // Builtin (e.g. `default`) is loaded read-only — server enforces, but we
      // also surface the read-only state in the UI to avoid noisy 403s.
      this.editor = {
        open: true,
        kind: row.kind,
        name: row.name,
        content: '',
        originalContent: '',
        submitting: false,
        error: '',
      };
      try {
        const res = await this._request(
          'GET', `/api/prompts/templates/${row.kind}/${encodeURIComponent(row.name)}`,
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this.editor.content         = data.content;
        this.editor.originalContent = data.content;
      } catch (e) {
        this.editor.error = 'Failed to load template: ' + e.message;
      }
    },

    closeEditor() {
      // Bail-out guard for unsaved edits — read the top-level getter
      // (`editorDirty`), not a phantom `editor.dirty` field that doesn't exist.
      if (this.editorDirty && !confirm('Discard unsaved changes?')) return;
      this.editor.open = false;
    },

    get isEditorBuiltin() {
      const list = this.rowsFor(this.editor.kind);
      const row = list.find(r => r.name === this.editor.name);
      return !!row?.is_builtin;
    },

    get editorDirty() {
      return this.editor.content !== this.editor.originalContent;
    },

    async saveEditor() {
      if (this.editor.submitting) return;
      this.editor.submitting = true;
      this.editor.error = '';
      try {
        const res = await this._request(
          'PUT',
          `/api/prompts/templates/${this.editor.kind}/${encodeURIComponent(this.editor.name)}`,
          { content: this.editor.content },
        );
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          this.editor.error = this._extractError(data, `HTTP ${res.status}`);
          this.showToast('Save failed: ' + this.editor.error, 'error');
          return;
        }
        const updated = await res.json();
        this._mergeRow(updated);
        this.editor.originalContent = this.editor.content;
        this.showToast('Template saved', 'success');
        this.editor.open = false;
      } catch (e) {
        this.editor.error = e.message;
        this.showToast('Network error: ' + e.message, 'error');
      } finally {
        this.editor.submitting = false;
      }
    },

    // ── Create (+ New …) ───────────────────────────────────
    openCreate(kind, source = '') {
      this.create = {
        open: true,
        kind,
        name: '',
        source,
        submitting: false,
        error: '',
      };
    },

    closeCreate() { this.create.open = false; },

    async submitCreate() {
      if (this.create.submitting) return;
      const name = (this.create.name || '').trim();
      if (!name) { this.create.error = 'name is required'; return; }
      this.create.submitting = true;
      this.create.error = '';
      try {
        const body = { name };
        if (this.create.source) body.source = this.create.source;
        const res = await this._request(
          'POST', `/api/prompts/templates/${this.create.kind}`, body,
        );
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          this.create.error = this._extractError(data, `HTTP ${res.status}`);
          this.showToast('Create failed: ' + this.create.error, 'error');
          return;
        }
        const row = await res.json();
        this._mergeRow(row);
        this.showToast(`Template '${row.name}' created`, 'success');
        this.create.open = false;
      } catch (e) {
        this.create.error = e.message;
        this.showToast('Network error: ' + e.message, 'error');
      } finally {
        this.create.submitting = false;
      }
    },

    duplicate(row) {
      this.openCreate(row.kind, row.name);
    },

    // ── Rename ─────────────────────────────────────────────
    openRename(row) {
      this.rename = {
        open: true,
        kind: row.kind,
        oldName: row.name,
        newName: '',
        submitting: false,
        error: '',
      };
    },

    closeRename() { this.rename.open = false; },

    async submitRename() {
      if (this.rename.submitting) return;
      const newName = (this.rename.newName || '').trim();
      if (!newName) { this.rename.error = 'new name is required'; return; }
      if (newName === this.rename.oldName) {
        this.rename.error = 'new name is the same as the old one';
        return;
      }
      this.rename.submitting = true;
      this.rename.error = '';
      try {
        const res = await this._request(
          'POST',
          `/api/prompts/templates/${this.rename.kind}/${encodeURIComponent(this.rename.oldName)}/rename`,
          { new_name: newName },
        );
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          this.rename.error = this._extractError(data, `HTTP ${res.status}`);
          this.showToast('Rename failed: ' + this.rename.error, 'error');
          return;
        }
        const row = await res.json();
        this._removeRow(this.rename.kind, this.rename.oldName);
        this._mergeRow(row);
        this.showToast(`Renamed to '${row.name}'`, 'success');
        this.rename.open = false;
        // Cascade may have changed intent rows' template references; ask for a full refresh
        // so ref_count / ref_intents stay in sync. R5 (frontend optimistic update + 409 race).
        await this.loadList();
      } catch (e) {
        this.rename.error = e.message;
        this.showToast('Network error: ' + e.message, 'error');
      } finally {
        this.rename.submitting = false;
      }
    },

    // ── Delete ─────────────────────────────────────────────
    openDelete(row) {
      this.del = {
        open: true,
        kind: row.kind,
        name: row.name,
        refIntents: row.ref_intents || [],
        submitting: false,
        error: '',
      };
    },

    closeDelete() { this.del.open = false; },

    async submitDelete() {
      if (this.del.submitting) return;
      this.del.submitting = true;
      this.del.error = '';
      try {
        const res = await this._request(
          'DELETE',
          `/api/prompts/templates/${this.del.kind}/${encodeURIComponent(this.del.name)}`,
        );
        if (res.status === 204) {
          this._removeRow(this.del.kind, this.del.name);
          this.showToast(`Template '${this.del.name}' deleted`, 'success');
          this.del.open = false;
          return;
        }
        const data = await res.json().catch(() => ({}));
        if (res.status === 409) {
          // Race against an intent grabbing the template after our cached list.
          this.del.refIntents = data?.detail?.ref_intents || [];
          this.del.error = `This template is now referenced by ${this.del.refIntents.length} intent(s) — refresh to see them.`;
          this.showToast(this.del.error, 'error');
          await this.loadList();
          return;
        }
        this.del.error = this._extractError(data, `HTTP ${res.status}`);
        this.showToast('Delete failed: ' + this.del.error, 'error');
      } catch (e) {
        this.del.error = e.message;
        this.showToast('Network error: ' + e.message, 'error');
      } finally {
        this.del.submitting = false;
      }
    },

    // ── Local list mutators (avoid full GET round-trip on success) ─────────
    _mergeRow(row) {
      const list = row.kind === 'system' ? this.system : this.instruction;
      const idx = list.findIndex(r => r.name === row.name);
      if (idx >= 0) list.splice(idx, 1, row);
      else list.push(row);
      list.sort((a, b) => a.name.localeCompare(b.name));
    },

    _removeRow(kind, name) {
      const list = kind === 'system' ? this.system : this.instruction;
      const idx = list.findIndex(r => r.name === name);
      if (idx >= 0) list.splice(idx, 1);
    },

    // ── UI helpers ─────────────────────────────────────────
    showToast(msg, type = 'info') {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, msg, type });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 4000);
    },

    fmtMtime(mtime) {
      // mtime is a UNIX epoch float (seconds).
      if (typeof window.fmtDateTime === 'function') return window.fmtDateTime(mtime);
      const d = new Date(mtime * 1000);
      return isNaN(d.getTime()) ? '—' : d.toISOString();
    },

    fmtSize(b) {
      if (b == null) return '—';
      if (b < 1024) return `${b} B`;
      return `${(b / 1024).toFixed(1)} KiB`;
    },
  };
}
