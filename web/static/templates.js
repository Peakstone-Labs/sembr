/* templatesTab() — Alpine 3 component for the Templates management view.
 *
 * Layout: left tree of templates (system / instruction groups) + right inline editor.
 * Selection: `editor.kind` + `editor.name` together identify the loaded file; both
 * empty means "nothing selected, show empty-state". Create / Rename / Delete remain
 * as small modal forms triggered from the right-pane action row.
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

    // editor === null kind/name means nothing selected → right pane shows the
    // empty-state placeholder. submitting/error are scoped to the active save.
    editor: {
      kind: null,        // 'system' | 'instruction' | null
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
      refIntents: [],    // populated from row's ref_intents on open OR from server 409
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
        // Drop selection if the previously-selected file no longer exists.
        if (this.editor.kind && this.editor.name && !this.currentRow) {
          this.clearEditor();
        }
      } catch (e) {
        this.showToast('Failed to load templates: ' + e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    rowsFor(kind) {
      return kind === 'system' ? this.system : this.instruction;
    },

    // ── Selection / Editor ─────────────────────────────────
    get currentRow() {
      if (!this.editor.kind || !this.editor.name) return null;
      return this.rowsFor(this.editor.kind).find(r => r.name === this.editor.name) || null;
    },

    get isSelected() {
      return !!(this.editor.kind && this.editor.name && this.currentRow);
    },

    get isEditorBuiltin() {
      return !!this.currentRow?.is_builtin;
    },

    get editorDirty() {
      return this.editor.content !== this.editor.originalContent;
    },

    // Markdown highlight for the overlay editor (shared impl, codehl.js).
    highlightMarkdown(text) { return window.ceHighlightMarkdown(text); },

    isActive(kind, name) {
      return this.editor.kind === kind && this.editor.name === name;
    },

    clearEditor() {
      this.editor = {
        kind: null,
        name: '',
        content: '',
        originalContent: '',
        submitting: false,
        error: '',
      };
    },

    revertEditor() {
      this.editor.content = this.editor.originalContent;
      this.editor.error = '';
    },

    async select(row) {
      // Bail-out guard for unsaved edits when switching files.
      if (this.editorDirty && !confirm('Discard unsaved changes?')) return;
      this.editor = {
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

    async saveEditor() {
      if (this.editor.submitting || this.isEditorBuiltin || !this.editorDirty) return;
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
      if (!name) { this.create.error = 'Name is required'; return; }
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
        // Auto-select the newly created file in the right pane.
        await this.select(row);
      } catch (e) {
        this.create.error = e.message;
        this.showToast('Network error: ' + e.message, 'error');
      } finally {
        this.create.submitting = false;
      }
    },

    duplicateCurrent() {
      if (!this.currentRow) return;
      this.openCreate(this.currentRow.kind, this.currentRow.name);
    },

    // ── Rename ─────────────────────────────────────────────
    openRenameCurrent() {
      if (!this.currentRow || this.currentRow.is_builtin) return;
      this.rename = {
        open: true,
        kind: this.currentRow.kind,
        oldName: this.currentRow.name,
        newName: '',
        submitting: false,
        error: '',
      };
    },

    closeRename() { this.rename.open = false; },

    async submitRename() {
      if (this.rename.submitting) return;
      const newName = (this.rename.newName || '').trim();
      if (!newName) { this.rename.error = 'New name is required'; return; }
      if (newName === this.rename.oldName) {
        this.rename.error = 'New name is the same as the old one';
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
        const renamingActive = (
          this.editor.kind === this.rename.kind &&
          this.editor.name === this.rename.oldName
        );
        this._removeRow(this.rename.kind, this.rename.oldName);
        this._mergeRow(row);
        this.showToast(`Renamed to '${row.name}'`, 'success');
        this.rename.open = false;
        // Cascade may have changed intent rows' template references; refresh so
        // ref_count / ref_intents stay in sync with reality (R5 for the read view).
        await this.loadList();
        // Re-point the editor selection so the right pane keeps showing the file.
        if (renamingActive) {
          this.editor.name = row.name;
        }
      } catch (e) {
        this.rename.error = e.message;
        this.showToast('Network error: ' + e.message, 'error');
      } finally {
        this.rename.submitting = false;
      }
    },

    // ── Delete ─────────────────────────────────────────────
    openDeleteCurrent() {
      if (!this.currentRow || this.currentRow.is_builtin) return;
      this.del = {
        open: true,
        kind: this.currentRow.kind,
        name: this.currentRow.name,
        refIntents: this.currentRow.ref_intents || [],
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
          const wasActive = (
            this.editor.kind === this.del.kind &&
            this.editor.name === this.del.name
          );
          this._removeRow(this.del.kind, this.del.name);
          this.showToast(`Template '${this.del.name}' deleted`, 'success');
          this.del.open = false;
          if (wasActive) this.clearEditor();
          return;
        }
        const data = await res.json().catch(() => ({}));
        if (res.status === 409) {
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
