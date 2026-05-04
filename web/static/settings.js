/* Alpine component for the Settings tab.
 *
 * Calls /api/settings/* — these endpoints REQUIRE the X-Dashboard-Token header
 * (no cookie fallback) for CSRF protection. We always pull the token from
 * localStorage (same convention as app.js) and pass it explicitly.
 */

window.SENSITIVE_MASK = '••••••';

function settingsTab() {
  return {
    loading: true,
    error: '',
    schema: { sembr_fields: [], passthrough_prefixes: [] },
    initialValues: {},          // server's view of current .env values (mask in place of secrets)
    overriddenByShellEnv: [],
    unknownKeys: [],
    form: {},                   // user-editable copy of initialValues
    deletions: [],
    newKey: '',
    newValue: '',
    addError: '',
    sensitiveMask: window.SENSITIVE_MASK,
    confirm: { open: false, submitting: false },
    diff: { changes: [], additions: [], deletions: [], touchesPassthrough: false },
    overriddenSubmittedFields: [],
    restart: { active: false, message: '', startedAt: 0, elapsedMs: 0, timer: null },
    toasts: [],

    async init() {
      await this._reload();
    },

    async _reload() {
      this.loading = true;
      this.error = '';
      try {
        const [schema, values] = await Promise.all([
          this._fetch('/api/settings/schema'),
          this._fetch('/api/settings/values'),
        ]);
        this.schema = schema;
        this.initialValues = { ...values.values };
        this.form = { ...values.values };
        this.overriddenByShellEnv = values.overridden_by_shell_env || [];
        this.unknownKeys = values.unknown_keys || [];
        this.deletions = [];
      } catch (e) {
        this.error = `Failed to load settings: ${e.message}`;
      } finally {
        this.loading = false;
      }
    },

    _token() {
      try { return localStorage.getItem('sembr_dashboard_token') || ''; }
      catch (e) { return ''; }
    },

    async _fetch(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      const t = this._token();
      if (t) headers['X-Dashboard-Token'] = t;
      if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
      const res = await fetch(path, { ...options, headers });
      if (res.status === 401) {
        window.location.href = '/dashboard/login.html';
        throw new Error('unauthorized');
      }
      const text = await res.text();
      let body;
      try { body = text ? JSON.parse(text) : null; }
      catch (e) { body = text; }
      if (!res.ok) {
        const detail = body && body.detail ? body.detail : `HTTP ${res.status}`;
        const message = typeof detail === 'string' ? detail : JSON.stringify(detail);
        throw new Error(message);
      }
      return body;
    },

    isSecretLike(key) {
      const upper = (key || '').toUpperCase();
      return /(TOKEN|COOKIE|SECRET|KEY|PASSWORD|SESSION)/.test(upper);
    },

    passthroughKeysPresent() {
      const sembrKeys = new Set(this.schema.sembr_fields.map(f => f.key));
      return Object.keys(this.initialValues).filter(k => !sembrKeys.has(k));
    },

    isPassthroughKey(key) {
      const upper = (key || '').toUpperCase();
      if (!/^[A-Z][A-Z0-9_]*$/.test(upper)) return false;
      return this.schema.passthrough_prefixes.some(p => upper.startsWith(p));
    },

    addPassthrough() {
      this.addError = '';
      const upper = (this.newKey || '').trim().toUpperCase();
      if (!upper) return;
      if (!this.isPassthroughKey(upper)) {
        this.addError = `key must match ^[A-Z][A-Z0-9_]*$ and begin with one of: ` +
                        this.schema.passthrough_prefixes.join(', ');
        return;
      }
      if (this.form[upper] !== undefined) {
        this.addError = `key already present`;
        return;
      }
      this.form[upper] = this.newValue;
      // Make it appear in passthroughKeysPresent() by also seeding initialValues
      // with an empty marker — server treats absent → addition automatically.
      this.initialValues[upper] = '__pending_addition__';
      this.newKey = '';
      this.newValue = '';
    },

    markDeleted(key) {
      if (!this.deletions.includes(key)) this.deletions.push(key);
      delete this.form[key];
      // Visually remove the row by removing the initialValues entry.
      delete this.initialValues[key];
    },

    isDirty() {
      if (this.deletions.length > 0) return true;
      for (const k of Object.keys(this.form)) {
        const fresh = !(k in this.initialValues) || this.initialValues[k] === '__pending_addition__';
        if (fresh) return true;
        if (this.form[k] !== this.initialValues[k]) return true;
      }
      return false;
    },

    _computeDiff() {
      const sembrKeys = new Set(this.schema.sembr_fields.map(f => f.key));
      const changes = {};
      const additions = {};

      for (const key of Object.keys(this.form)) {
        const isAddition = !(key in this.initialValues) || this.initialValues[key] === '__pending_addition__';
        const value = this.form[key];
        if (isAddition) {
          additions[key] = value === undefined ? '' : String(value);
        } else if (value !== this.initialValues[key]) {
          changes[key] = value === undefined ? '' : String(value);
        }
      }

      const touchesPassthrough =
        Object.keys(changes).some(k => !sembrKeys.has(k)) ||
        Object.keys(additions).some(k => !sembrKeys.has(k)) ||
        this.deletions.some(k => !sembrKeys.has(k));

      return {
        changes: Object.keys(changes).map(k => ({ k, v: changes[k] })),
        additions: Object.keys(additions).map(k => ({ k, v: additions[k] })),
        deletions: this.deletions.slice(),
        touchesPassthrough,
        _changesObj: changes,
        _additionsObj: additions,
      };
    },

    openConfirm() {
      const d = this._computeDiff();
      if (d.changes.length === 0 && d.additions.length === 0 && d.deletions.length === 0) {
        this._toast('warn', 'no changes');
        return;
      }
      this.diff = d;
      // Identify shell-overridden fields that are being submitted, so we can
      // warn the user (Decision #12).
      const submittedKeys = new Set([...Object.keys(d._changesObj), ...Object.keys(d._additionsObj)]);
      this.overriddenSubmittedFields = this.overriddenByShellEnv.filter(k => submittedKeys.has(k));
      this.confirm.open = true;
    },

    closeConfirm() {
      this.confirm.open = false;
    },

    async submit() {
      this.confirm.submitting = true;
      try {
        const body = {
          changes: this.diff._changesObj,
          additions: this.diff._additionsObj,
          deletions: this.diff.deletions,
          confirmed: true,
        };
        const res = await this._fetch('/api/settings/save', {
          method: 'POST',
          body: JSON.stringify(body),
        });
        this.confirm.open = false;
        const targets = res.restart_targets || [];
        if (targets.length === 0) {
          this._toast('ok', 'saved (no restart needed)');
          await this._reload();
        } else {
          this._beginRestart(targets);
        }
      } catch (e) {
        this._toast('error', `save failed: ${e.message}`);
      } finally {
        this.confirm.submitting = false;
      }
    },

    _beginRestart(targets) {
      this.restart.active = true;
      this.restart.startedAt = Date.now();
      this.restart.elapsedMs = 0;
      this.restart.message = `restarting ${targets.join(' + ')}…`;
      // Polling loop. Wait for /health to respond 200 again.
      this.restart.timer = setInterval(() => this._pollRestart(), 1000);
    },

    async _pollRestart() {
      this.restart.elapsedMs = Date.now() - this.restart.startedAt;
      if (this.restart.elapsedMs > 60000) {
        clearInterval(this.restart.timer);
        this.restart.active = false;
        this._toast('error', 'restart timed out (60s) — check container logs');
        return;
      }
      try {
        const res = await fetch('/health', { cache: 'no-store' });
        if (res.status === 200) {
          clearInterval(this.restart.timer);
          this.restart.active = false;
          this._toast('ok', 'restart complete');
          await this._reload();
        }
      } catch (e) {
        // expected during the restart window
      }
    },

    waitForRestart() {
      // public alias used in tests / external scripts
      return this._pollRestart();
    },

    _toast(type, msg) {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, type, msg });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, 4000);
    },
  };
}

window.settingsTab = settingsTab;
