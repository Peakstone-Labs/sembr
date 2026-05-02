/* intentsTab() — Alpine 3 component for the Intents management view.
 *
 * Consumes: GET/POST/PUT/DELETE /intents, POST/GET /intents/{id}/fire/{task_id},
 *           GET /feeds, GET /api/prompts/templates
 * Auth: X-Dashboard-Token header (localStorage); cookie path does not cover these paths.
 * Design: see sembr-dev-docs/development/intent-management-tab/design.md
 */

function intentsTab() {
  return {
    // ── State ──────────────────────────────────────────────
    list: [],
    cronList: [],
    eventList: [],
    subTab: 'cron',
    loading: false,

    feeds: [],
    systemTemplates: [],
    instructionTemplates: [],

    modal: {
      open: false,
      mode: 'create',       // 'create' | 'edit'
      intentMode: 'cron',   // 'cron' | 'event'
      editId: null,
      submitting: false,
      errors: {},
      form: {},
    },

    fire: {
      open: false,
      phase: 'form',        // 'form' | 'running' | 'result'
      intent: null,
      form: { lookback: 86400, skip_seen: true, threshold: 0.75 },
      taskId: null,
      statusUrl: null,
      result: null,
      error: null,
      _timer: null,
    },

    del: {
      open: false,
      intentId: null,
      intentName: '',
    },

    toasts: [],

    // ── Lifecycle ──────────────────────────────────────────
    async init() {
      this._initialized = false;
      this.subTab = window.location.hash.includes('/event') ? 'event' : 'cron';
      // Lazy-load: only hit the API when the Intents tab is actually visited.
      // Handles deep-link (#intents/...) on first load AND deferred tab switch.
      const maybeLoad = async () => {
        if (window.location.hash.startsWith('#intents') && !this._initialized) {
          this._initialized = true;
          await Promise.all([this.loadList(), this.loadFeeds(), this.loadTemplates()]);
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

    // Normalise Pydantic v2 detail (array) or plain string into a human-readable message.
    _extractError(data, fallback) {
      if (typeof data?.detail === 'string') return data.detail;
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
        const res = await this._request('GET', '/intents');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.list = await res.json();
        this.cronList  = this.list.filter(i => i.schedule?.mode === 'cron');
        this.eventList = this.list.filter(i => i.schedule?.mode === 'event');
      } catch (e) {
        this.showToast('Failed to load intents: ' + e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    async loadFeeds() {
      try {
        const res = await this._request('GET', '/feeds');
        if (!res.ok) return;
        const data = await res.json();
        this.feeds = Array.isArray(data) ? data : (data.feeds || []);
      } catch (_) { /* feeds are optional for feed_filter — fail silently */ }
    },

    async loadTemplates() {
      try {
        const res = await this._request('GET', '/api/prompts/templates');
        if (!res.ok) throw new Error();
        const data = await res.json();
        this.systemTemplates = data.system || [];
        this.instructionTemplates = data.instruction || [];
      } catch (_) {
        this.systemTemplates = [];
        this.instructionTemplates = [];
      }
    },

    // ── Sub-tab switching ──────────────────────────────────
    setSubTab(sub) {
      this.subTab = sub;
      window.location.hash = 'intents/' + sub;
    },

    // ── Default form factory ───────────────────────────────
    _defaultForm() {
      return {
        name: '', text: '',
        threshold: 0.75,
        enabled: true,
        language: 'zh',
        timezone: 'Asia/Shanghai',
        tags: [], tagInput: '',
        // cron schedule
        preset: 'daily', hour: 0, minute: 0, weekday: 'mon',
        lookback_seconds: 86400, skip_seen: true,
        // event schedule
        trigger_count: 3, max_wait_seconds: 1800,
        // templates
        system_template: 'default', instruction_template: 'default',
        // feed filter
        feedFilterMode: 'all', feedIds: [],
        // channels
        toEmails: [], toInput: '',
        ccEmails: [], ccInput: '',
        bccEmails: [], bccInput: '',
      };
    },

    // ── Modal open/close ───────────────────────────────────
    openCreate(mode) {
      this.modal = {
        open: true, mode: 'create', intentMode: mode,
        editId: null, submitting: false, errors: {},
        form: this._defaultForm(),
      };
    },

    openEdit(intent) {
      const s  = intent.schedule || {};
      const ch = (intent.channels || [])[0] || {};
      const ff = intent.feed_filter;
      this.modal = {
        open: true, mode: 'edit', intentMode: s.mode || 'cron',
        editId: intent.id, submitting: false, errors: {},
        form: {
          name: intent.name,
          text: intent.text,
          threshold: intent.threshold,
          enabled: intent.enabled,
          language: intent.language || 'zh',
          timezone: intent.timezone || 'Asia/Shanghai',
          tags: [...(intent.tags || [])],
          tagInput: '',
          // cron fields (used when intentMode==='cron')
          preset:           s.preset || 'daily',
          hour:             s.hour   ?? 0,
          minute:           s.minute ?? 0,
          weekday:          s.weekday || 'mon',
          lookback_seconds: s.lookback_seconds ?? 86400,
          skip_seen:        s.skip_seen ?? true,
          // event fields
          trigger_count:    s.trigger_count    ?? 3,
          max_wait_seconds: s.max_wait_seconds ?? 1800,
          // templates
          system_template:      intent.system_template      || 'default',
          instruction_template: intent.instruction_template || 'default',
          // feed filter: null or {ids:null} → all (全扫); {ids:[...]} → specific
          feedFilterMode: (ff === null || ff === undefined || ff.ids === null || ff.ids === undefined) ? 'all' : 'specific',
          feedIds: ff?.ids ? [...ff.ids] : [],
          // channels
          toEmails: [...(ch.to  || [])], toInput: '',
          ccEmails: [...(ch.cc  || [])], ccInput: '',
          bccEmails: [...(ch.bcc || [])], bccInput: '',
        },
      };
    },

    closeModal() {
      this.modal.open = false;
    },

    // ── Tag management ─────────────────────────────────────
    addTag() {
      const t = this.modal.form.tagInput.trim();
      if (!t) return;
      if (t.length > 50) { this.showToast('Tag too long (max 50 chars)', 'error'); return; }
      if (this.modal.form.tags.length >= 10) { this.showToast('Max 10 tags', 'error'); return; }
      if (!this.modal.form.tags.includes(t)) this.modal.form.tags.push(t);
      this.modal.form.tagInput = '';
    },

    removeTag(idx) {
      this.modal.form.tags.splice(idx, 1);
    },

    handleTagKeydown(e) {
      if (e.key === 'Enter') { e.preventDefault(); this.addTag(); }
    },

    // ── Email tag management ───────────────────────────────
    addEmail(field) {
      const k   = field + 'Input';
      const lk  = field + 'Emails';
      const val = this.modal.form[k].trim().toLowerCase();
      if (!val) { delete this.modal.errors[k]; return; }
      if (!this._isEmail(val)) {
        this.modal.errors[k] = 'Invalid email address';
        return;
      }
      delete this.modal.errors[k];
      if (!this.modal.form[lk].includes(val)) this.modal.form[lk].push(val);
      this.modal.form[k] = '';
    },

    removeEmail(field, idx) {
      this.modal.form[field + 'Emails'].splice(idx, 1);
    },

    handleEmailKeydown(e, field) {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        this.addEmail(field);
      } else if (e.key === 'Backspace' && !this.modal.form[field + 'Input']) {
        const list = this.modal.form[field + 'Emails'];
        if (list.length) list.pop();
      }
    },

    _isEmail(v) {
      return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
    },

    // ── Feed filter ────────────────────────────────────────
    toggleFeedId(id) {
      const idx = this.modal.form.feedIds.indexOf(id);
      if (idx === -1) this.modal.form.feedIds.push(id);
      else this.modal.form.feedIds.splice(idx, 1);
    },

    // ── Form validation ────────────────────────────────────
    _validate() {
      const errors = {};
      const f = this.modal.form;
      if (!f.name.trim())          errors.name = 'Required';
      else if (f.name.length > 100) errors.name = 'Max 100 characters';

      if (!f.text.trim())           errors.text = 'Required';
      else if (f.text.length > 2000) errors.text = 'Max 2000 characters';

      const thr = parseFloat(f.threshold);
      if (isNaN(thr) || thr < 0.60 || thr > 0.95)
        errors.threshold = 'Must be between 0.60 and 0.95';



      if (!f.language.trim())
        errors.language = 'Required';
      else if (f.language.length > 32)
        errors.language = 'Max 32 characters';
      else if (!/^[A-Za-z][A-Za-z0-9_\- ]*$/.test(f.language))
        errors.language = 'Must start with a letter; letters/digits/hyphens/underscores only';

      if (this.modal.intentMode === 'cron') {
        if (f.preset === 'weekly' && !f.weekday)
          errors.weekday = 'Required for weekly preset';
        const lb = parseInt(f.lookback_seconds);
        if (isNaN(lb) || lb < 300 || lb > 2592000)
          errors.lookback_seconds = 'Must be 300–2592000 seconds';
      } else {
        const tc = parseInt(f.trigger_count);
        if (isNaN(tc) || tc < 1 || tc > 10)
          errors.trigger_count = 'Must be 1–10';
        const mw = parseInt(f.max_wait_seconds);
        if (isNaN(mw) || mw < 60 || mw > 86400)
          errors.max_wait_seconds = 'Must be 60–86400 seconds';
      }

      if (f.toEmails.length === 0) errors.to = 'At least one recipient required';
      return errors;
    },

    // ── Submit intent (create or update) ───────────────────
    async submitIntent() {
      // Flush any pending tag/email text
      if (this.modal.form.tagInput.trim())  this.addTag();
      if (this.modal.form.toInput.trim())   this.addEmail('to');
      if (this.modal.form.ccInput.trim())   this.addEmail('cc');
      if (this.modal.form.bccInput.trim())  this.addEmail('bcc');

      const errs = this._validate();
      if (Object.keys(errs).length) { this.modal.errors = errs; return; }
      this.modal.errors = {};

      const f = this.modal.form;
      const schedule = this.modal.intentMode === 'cron'
        ? {
          mode:             'cron',
          preset:           f.preset,
          hour:             f.preset === 'hourly' ? 0 : (parseInt(f.hour)   || 0),
          minute:           parseInt(f.minute) || 0,
          weekday:          f.preset === 'weekly' ? f.weekday : null,
          lookback_seconds: parseInt(f.lookback_seconds) || 86400,
          skip_seen:        !!f.skip_seen,
        }
        : {
          mode:             'event',
          trigger_count:    parseInt(f.trigger_count)    || 3,
          max_wait_seconds: parseInt(f.max_wait_seconds) || 1800,
        };

      const payload = {
        name:                 f.name.trim(),
        text:                 f.text.trim(),
        threshold:            parseFloat(f.threshold),
        enabled:              !!f.enabled,
        language:             f.language.trim(),
        timezone:             f.timezone.trim(),
        tags:                 f.tags,
        schedule,
        system_template:      f.system_template      || 'default',
        instruction_template: f.instruction_template || 'default',
        feed_filter:          f.feedFilterMode === 'all' ? null : { ids: f.feedIds },
        channels: [{
          type: 'email',
          to:   f.toEmails,
          cc:   f.ccEmails,
          bcc:  f.bccEmails,
        }],
      };

      const isCreate = this.modal.mode === 'create';
      const editId   = this.modal.editId;
      this.modal.submitting = true;
      try {
        const res = isCreate
          ? await this._request('POST', '/intents', payload)
          : await this._request('PUT',  `/intents/${editId}`, payload);

        if (res.ok) {
          this.closeModal();
          await this.loadList();
          this.showToast(isCreate ? 'Intent created' : 'Intent updated', 'success');
        } else {
          const data = await res.json().catch(() => ({}));
          if (res.status === 422 && Array.isArray(data.detail)) {
            const fe = {};
            for (const err of data.detail) {
              const tail = (err.loc || []).slice(-1)[0];
              // Integer tails (e.g. channel list index) can't map to a named field.
              const field = (typeof tail === 'string') ? tail : null;
              if (field && (field in this.modal.form || field === 'to')) {
                fe[field] = err.msg;
              } else {
                const path = (err.loc || []).join('.');
                fe._global = (fe._global ? fe._global + '; ' : '') + `${path}: ${err.msg}`;
              }
            }
            this.modal.errors = fe;
            if (fe._global) this.showToast('Validation error: ' + fe._global, 'error');
          } else {
            this.showToast('Error: ' + this._extractError(data, `HTTP ${res.status}`), 'error');
          }
        }
      } catch (e) {
        this.showToast('Network error: ' + e.message, 'error');
      } finally {
        this.modal.submitting = false;
      }
    },

    // ── Delete ─────────────────────────────────────────────
    confirmDelete(intent) {
      this.del = { open: true, intentId: intent.id, intentName: intent.name };
    },

    closeDelete() {
      this.del = { open: false, intentId: null, intentName: '' };
    },

    async deleteIntent() {
      const id = this.del.intentId;
      this.closeDelete();
      try {
        const res = await this._request('DELETE', `/intents/${id}`);
        if (res.ok || res.status === 204 || res.status === 404) {
          await this.loadList();
          this.showToast('Intent deleted', 'success');
        } else {
          const data = await res.json().catch(() => ({}));
          this.showToast('Delete failed: ' + this._extractError(data, res.status), 'error');
        }
      } catch (e) {
        this.showToast('Network error: ' + e.message, 'error');
      }
    },

    // ── Enable / disable toggle ────────────────────────────
    async toggleEnabled(intent) {
      const id   = intent.id;
      const prev = intent.enabled;
      intent.enabled = !prev;
      try {
        const res = await this._request('PUT', `/intents/${id}`, { enabled: intent.enabled });
        if (!res.ok) {
          // Rollback by id in case loadList() replaced the array while PUT was in flight.
          const cur = this.list.find(i => i.id === id);
          if (cur) cur.enabled = prev;
          const data = await res.json().catch(() => ({}));
          this.showToast('Update failed: ' + this._extractError(data, res.status), 'error');
        }
      } catch (e) {
        const cur = this.list.find(i => i.id === id);
        if (cur) cur.enabled = prev;
        this.showToast('Network error: ' + e.message, 'error');
      }
    },

    // ── Fire dialog ────────────────────────────────────────
    openFire(intent) {
      const s = intent.schedule || {};
      if (this.fire._timer) clearTimeout(this.fire._timer);
      this.fire = {
        open: true, phase: 'form', intent,
        form: {
          lookback:  s.lookback_seconds ?? 86400,
          skip_seen: s.skip_seen        ?? true,
          threshold: intent.threshold   ?? 0.75,
        },
        taskId: null, statusUrl: null, result: null, error: null, _timer: null,
      };
    },

    closeFire() {
      if (this.fire._timer) clearTimeout(this.fire._timer);
      this.fire.open = false;
    },

    async runFire() {
      const { intent, form } = this.fire;
      // Client-side bounds check (mirrors backend Query constraints)
      this.fire.error = null;
      const lb = parseInt(form.lookback);
      if (isNaN(lb) || lb < 300 || lb > 2592000) {
        this.fire.error = 'Lookback must be between 300 and 2592000 seconds.'; return;
      }
      const thr = parseFloat(form.threshold);
      if (isNaN(thr) || thr < 0.60 || thr > 0.95) {
        this.fire.error = 'Threshold must be between 0.60 and 0.95.'; return;
      }
      const qs = new URLSearchParams({
        lookback:  lb,
        skip_seen: form.skip_seen,
        threshold: thr,
      }).toString();
      this.fire.phase = 'running';
      try {
        const res = await this._request('POST', `/intents/${intent.id}/fire?${qs}`);
        if (res.status === 429) {
          this.fire.phase = 'form';
          this.showToast('Rate limited: only 1 fire per intent per 60 seconds', 'error');
          return;
        }
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          this.fire.phase = 'form';
          this.fire.error = this._extractError(data, `HTTP ${res.status}`);
          return;
        }
        const data = await res.json();
        this.fire.taskId    = data.task_id;
        this.fire.statusUrl = data.status_url;
        this._pollFire(0);
      } catch (e) {
        this.fire.phase = 'form';
        this.fire.error = 'Network error: ' + e.message;
      }
    },

    _pollFire(elapsed) {
      if (elapsed >= 60000) {
        this.fire.phase  = 'result';
        this.fire.result = null;
        this.fire.error  = 'Timeout after 60 s — task still running in background. '
          + 'Check backend logs or wait for the notification email.';
        return;
      }
      this.fire._timer = setTimeout(async () => {
        try {
          const res = await this._request('GET', this.fire.statusUrl);
          if (!res.ok) {
            this.fire.phase = 'result';
            this.fire.error = `Poll error: HTTP ${res.status}`;
            return;
          }
          const data = await res.json();
          if (data.status === 'done' || data.status === 'error') {
            this.fire.phase  = 'result';
            this.fire.result = data;
            if (data.status === 'error')
              this.fire.error = 'Task failed — check backend logs.';
          } else {
            this._pollFire(elapsed + 1000);
          }
        } catch (e) {
          this.fire.phase = 'result';
          this.fire.error = 'Network error during poll: ' + e.message;
        }
      }, 1000);
    },

    // ── Toast helpers ──────────────────────────────────────
    showToast(msg, type = 'info') {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, msg, type });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 4000);
    },

    // ── Display helpers ────────────────────────────────────
    fmtSchedule(intent) {
      const s = intent.schedule;
      if (!s) return '—';
      if (s.mode === 'event')
        return `event · ≥${s.trigger_count} matches`;
      const h = String(s.hour   ?? 0).padStart(2, '0');
      const m = String(s.minute ?? 0).padStart(2, '0');
      if (s.preset === 'hourly')  return 'every hour';
      if (s.preset === 'daily')   return `daily ${h}:${m}`;
      if (s.preset === 'weekly')  return `weekly ${s.weekday} ${h}:${m}`;
      return s.preset || '—';
    },
  };
}
