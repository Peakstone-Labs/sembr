/* Alpine component for the Settings tab.
 *
 * Calls /api/settings/* — these endpoints REQUIRE the X-Dashboard-Token header
 * (no cookie fallback) for CSRF protection. We always pull the token from
 * localStorage (same convention as app.js) and pass it explicitly.
 */

window.SENSITIVE_MASK = '••••••';

// Section ordering. Sorted by setup-necessity + change-frequency: required
// + frequently-touched sections at top; defaults-just-work sections at bottom.
// First match wins; unmatched fields fall through to "Other".
//
// `special: 'rsshub'` is a marker (no fields) — the template renders the
// passthrough KV editor at this slot instead of the standard sembr-fields
// block. Lets RSSHub Passthrough appear inline at position 3 instead of
// being pinned to the bottom.
const SECTION_DEFS = [
  { id: 'embedder',    title: 'Embedder',           prefixes: ['EMBEDDER_'] },
  { id: 'llm',         title: 'LLM Settings',       prefixes: ['LLM_'],
    exact: ['REDUCE_MODEL', 'META_EXTRACTION_MODEL', 'REDUCE_CONCURRENCY',
            'KB_MERGE_MODEL', 'KB_DISTILL_MODEL'] },
  { id: 'newsapi',     title: 'NewsAPI',            prefixes: ['NEWSAPI_'] },
  { id: 'rsshub',      title: 'RSSHub Passthrough', special: 'rsshub' },
  { id: 'smtp',        title: 'Email (SMTP)',       prefixes: ['SMTP_'] },
  { id: 'dashboard',   title: 'Dashboard',          prefixes: ['DASHBOARD_'] },
  // Maintenance must come BEFORE storage so QDRANT_NEWS_RETENTION_DAYS
  // (a retention setting, not a connection setting) gets bucketed here
  // — first match wins.
  { id: 'maintenance', title: 'Maintenance',        prefixes: ['MAINTENANCE_', 'DEAD_ARTICLES_'], exact: ['QDRANT_NEWS_RETENTION_DAYS'] },
  { id: 'display',     title: 'Display & Prompts',  prefixes: ['DISPLAY_', 'PROMPTS_'], exact: ['PROMPTS_DIR'] },
  { id: 'lifespan',    title: 'Lifespan',           prefixes: ['LIFESPAN_'] },
  { id: 'proxy',       title: 'Proxy / Routing',    prefixes: ['PROXY_'] },
  { id: 'storage',     title: 'Storage',            prefixes: ['QDRANT_', 'SQLITE_'] },
];

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
    sections: [],               // computed [{ id, title, fields, special? }]
    expandedSections: {},       // { sectionId: bool } — accordion state
    expandedUnknown: false,
    confirm: { open: false, submitting: false },
    diff: { changes: [], additions: [], deletions: [], touchesPassthrough: false },
    overriddenSubmittedFields: [],
    secretsBeingCleared: [],
    restart: { active: false, message: '', startedAt: 0, elapsedMs: 0, timer: null, _inFlight: false },
    toasts: [],

    async init() {
      // Cross-tab restart lock (D10): when the dashboard tab POSTs
      // /api/dashboard/restart, this tab needs to disable its save/restart
      // button — the 'storage' event fires here because we're a different tab.
      window.addEventListener('storage', (e) => {
        if (e.key !== 'sembr_restart_in_flight') return;
        if (e.newValue === '1' && !this.restart.active) {
          this._beginRestartShared('restart in progress (other tab)');
        }
      });
      try {
        if (localStorage.getItem('sembr_restart_in_flight') === '1' && !this.restart.active) {
          this._beginRestartShared('restart in progress (other tab)');
        }
      } catch (e) {}
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
        // Pre-seed recommended RSSHub keys as starter rows when missing from .env.
        // The __pending_addition__ sentinel makes _computeDiff treat any user
        // input as an addition (server route handles addition vs change).
        for (const rec of (schema.passthrough_recommended || [])) {
          if (!(rec.key in this.initialValues)) {
            this.initialValues[rec.key] = '__pending_addition__';
            this.form[rec.key] = '';
          }
        }
        this.sections = this._groupFields(schema.sembr_fields);
        // Default: all sections collapsed. Expand any section that has a
        // shell-overridden field, since the user usually wants to see those.
        const overrideKeys = new Set(this.overriddenByShellEnv);
        this.expandedSections = {};
        for (const sec of this.sections) {
          this.expandedSections[sec.id] = sec.fields.some(f => overrideKeys.has(f.key));
        }
      } catch (e) {
        this.error = `Failed to load settings: ${e.message}`;
      } finally {
        this.loading = false;
      }
    },

    _groupFields(fields) {
      const buckets = SECTION_DEFS.map(s => ({ ...s, fields: [] }));
      const otherBucket = { id: 'other', title: 'Other', fields: [] };

      outer: for (const f of fields) {
        for (let i = 0; i < SECTION_DEFS.length; i++) {
          const def = SECTION_DEFS[i];
          if (def.special) continue;  // markers don't bucket fields
          if ((def.exact || []).includes(f.key) ||
              (def.prefixes || []).some(p => f.key.startsWith(p))) {
            buckets[i].fields.push(f);
            continue outer;
          }
        }
        otherBucket.fields.push(f);
      }

      // Keep buckets that have fields OR are special markers (always rendered).
      const out = buckets.filter(b => b.fields.length > 0 || b.special);
      if (otherBucket.fields.length > 0) out.push(otherBucket);
      return out;
    },

    toggleSection(id) {
      this.expandedSections[id] = !this.expandedSections[id];
    },

    sectionDirtyCount(section) {
      let n = 0;
      for (const f of section.fields) {
        const a = this.form[f.key] === undefined || this.form[f.key] === null ? '' : String(this.form[f.key]);
        const b = this.initialValues[f.key] === undefined || this.initialValues[f.key] === null ? '' : String(this.initialValues[f.key]);
        if (a !== b) n++;
      }
      return n;
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

    // D13: helpers for the multiselect renderer. CSV ↔ array conversion
    // matches the Settings.newsapi_categories backend pattern (csv-with-comma
    // separator), so the field stays compatible with .env / shell-env input.
    multiselectValues(key) {
      const csv = this.form[key];
      if (csv === undefined || csv === null || csv === '') return [];
      return String(csv).split(',').map(s => s.trim()).filter(Boolean);
    },
    isMultiselectChecked(key, option) {
      return this.multiselectValues(key).includes(option);
    },
    toggleMultiselect(key, option, checked) {
      const set = new Set(this.multiselectValues(key));
      if (checked) set.add(option); else set.delete(option);
      // Preserve enum order rather than insertion order so the saved CSV is
      // deterministic across reloads.
      const enumOrder = ((this.schema.sembr_fields || []).find(f => f.key === key)?.enum) || [];
      const ordered = enumOrder.filter(o => set.has(o));
      this.form[key] = ordered.join(',');
    },

    passthroughKeysPresent() {
      const sembrKeys = new Set(this.schema.sembr_fields.map(f => f.key));
      // Include keys actually in .env AND recommended starter keys.
      const keys = Object.keys(this.initialValues).filter(k => !sembrKeys.has(k));
      // Sort: recommended (in declared order) first, then everything else.
      const recOrder = (this.schema.passthrough_recommended || []).map(r => r.key);
      const recIdx = (k) => {
        const i = recOrder.indexOf(k);
        return i === -1 ? Infinity : i;
      };
      return keys.sort((a, b) => recIdx(a) - recIdx(b) || a.localeCompare(b));
    },

    passthroughDescription(key) {
      const rec = (this.schema.passthrough_recommended || []).find(r => r.key === key);
      return rec ? rec.description : '';
    },

    isRecommendedPassthrough(key) {
      return (this.schema.passthrough_recommended || []).some(r => r.key === key);
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
        const a = this.form[k] === undefined || this.form[k] === null ? '' : String(this.form[k]);
        if (fresh) {
          // Empty fresh fields (recommended-but-unfilled) aren't dirty.
          if (a !== '') return true;
          continue;
        }
        const b = this.initialValues[k] === undefined || this.initialValues[k] === null ? '' : String(this.initialValues[k]);
        if (a !== b) return true;
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
        // 🟡-2: normalize both sides to string before comparing to dodge the
        // <input type=number> Number-coercion trap.
        const valueStr = value === undefined || value === null ? '' : String(value);
        const initStr = this.initialValues[key] === undefined || this.initialValues[key] === null
          ? '' : String(this.initialValues[key]);
        if (isAddition) {
          // Skip empty additions — recommended starter rows that the user
          // didn't fill should NOT be written to .env as empty values.
          if (valueStr === '') continue;
          additions[key] = valueStr;
        } else if (valueStr !== initStr) {
          changes[key] = valueStr;
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
      // 💡-2: detect "user is clearing a non-empty secret" so the confirm
      // dialog can flag this destructive action explicitly. Trigger when the
      // initial value was the mask sentinel (i.e. server had a real value)
      // and the new value is the empty string.
      this.secretsBeingCleared = Object.keys(d._changesObj).filter(k => {
        const wasMasked = this.initialValues[k] === this.sensitiveMask;
        const isEmpty = d._changesObj[k] === '';
        return wasMasked && isEmpty;
      });
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
        if (res.rsshub_restart_failed) {
          this._toast('error', `RSSHub restart failed: ${res.rsshub_error || 'unknown'} — fix manually`);
        }
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
      // D10 cross-tab lock: writer-side update is explicit because the
      // 'storage' event does NOT fire in the tab that called setItem.
      try { localStorage.setItem('sembr_restart_in_flight', '1'); } catch (e) {}
      this._beginRestartShared(`restarting ${targets.join(' + ')}…`);
    },

    _beginRestartShared(message) {
      if (this.restart.active) return;
      this.restart.active = true;
      this.restart.startedAt = Date.now();
      this.restart.elapsedMs = 0;
      this.restart.message = message;
      // Force-recreate path: helper container has a ~3.5s window where the
      // OLD api is still alive answering /health. We need to wait for the
      // OLD api to go down before treating a 200 from /health as "the new
      // api is up", otherwise _reload() reads stale env from the not-yet-
      // recreated container and shows phantom "overridden by shell env"
      // badges.
      this.restart.sawDown = false;
      this.restart.timer = setInterval(() => this._pollRestart(), 1000);
    },

    async _pollRestart() {
      if (this.restart._inFlight) return;
      this.restart.elapsedMs = Date.now() - this.restart.startedAt;
      if (this.restart.elapsedMs > 60000) {
        this._endRestart();
        this._toast('error', 'restart timed out (60s) — check container logs');
        return;
      }
      this.restart._inFlight = true;
      try {
        const res = await fetch('/health', { cache: 'no-store' });
        if (res.status === 200) {
          // Only accept "up" once we've actually observed the api going
          // down. Otherwise the still-alive pre-restart container flips us
          // straight to "restart complete" before the helper has stopped it.
          if (this.restart.sawDown) {
            this._endRestart();
            this._toast('ok', 'restart complete');
            await this._reload();
          }
        } else {
          this.restart.sawDown = true;
        }
      } catch (e) {
        // Network error / fetch failure means the api went away — exactly
        // the down signal we're waiting for.
        this.restart.sawDown = true;
      } finally {
        this.restart._inFlight = false;
      }
    },

    _endRestart() {
      if (this.restart.timer) clearInterval(this.restart.timer);
      this.restart.timer = null;
      this.restart.active = false;
      try { localStorage.removeItem('sembr_restart_in_flight'); } catch (e) {}
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
