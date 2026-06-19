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
      form: { lookback: 86400, skip_seen: true, threshold: 0.75, persist: false },
      taskId: null,
      statusUrl: null,
      result: null,
      error: null,
      _timer: null,
    },

    // History expansion (per-intent expand-row, cron sub-tab only)
    expandedIntentId: null,
    expanded: {
      kind: null,           // 'history' (room for future kinds)
      loading: false,
      rows: [],
      limit: 100,
      offset: 0,
      hasMore: false,
      error: null,
    },

    backfill: {
      open: false,
      phase: 'form',        // 'form' | 'running' | 'result'
      intent: null,
      form: { past_runs: 7 },
      taskId: null,
      statusUrl: null,
      progress: null,
      error: null,
      depthError: null,     // { oldest_date, max_backfillable_runs }
      _timer: null,
    },

    summarize: {
      open: false,
      intent: null,
      loading: false,
      form: { since: '', until: '', prompt: '' },
      result: null,          // { summary, rows_total, rows_used, rows_dropped }
      error: null,
      cachedSinceUntil: null, // { since, until } snapshot from last successful aggregate
      sendLoading: false,
    },

    export_: {
      open: false,
      intent: null,
      loading: false,
      form: { since: '', until: '' },
      error: null,
    },

    historyView: {
      open: false,
      row: null,            // selected summary_history row
      timezone: 'UTC',      // intent.timezone snapshot for run_at formatting
      summaryHtml: '',      // rendered markdown (DOMPurify-sanitized)
      citations: [],        // citation rows with optional .body / .bodyLoading
    },

    delHistory: {
      open: false,
      row: null,
      timezone: 'UTC',      // intent.timezone snapshot for run_at formatting
    },

    // ── Review gate state ──────────────────────────────────
    review: {
      open: false,
      running: false,
      row: null,
      timezone: 'UTC',
      error: null,
    },

    reviewCompare: {
      open: false,
      row: null,             // the history row being reviewed
      originalHtml: '',
      correctedHtml: '',
      correctedRaw: '',      // raw markdown for PATCH
      corrections: [],       // [{error_class, before, after, matched}]
      applying: false,
      error: null,
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
      // Server returns rich TemplateInfo rows since the template-mgmt rewrite
      // (D6); the intents tab only needs the names for the <select> options.
      try {
        const res = await this._request('GET', '/api/prompts/templates');
        if (!res.ok) throw new Error();
        const data = await res.json();
        this.systemTemplates = (data.system || []).map(r => r.name);
        this.instructionTemplates = (data.instruction || []).map(r => r.name);
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
        // intent-match-enhancement: each item is { language, text, translating } where
        // `translating` is a transient UI flag (loading spinner on the translate button)
        // and is stripped before POST/PUT body construction.
        subTexts: [],
        threshold: 0.75,
        enabled: true,
        language: 'zh',
        timezone: 'Asia/Shanghai',
        tags: [], tagInput: '',
        // cron schedule
        preset: 'daily', hour: 0, minute: 0, weekday: 'mon',
        lookback_hours: 24, skip_seen: true, history_days: '',
        // event schedule
        trigger_count: 3, max_wait_seconds: 1800,
        // templates
        system_template: 'default', instruction_template: 'default',
        review_gate: false,
        // feed filter
        feedFilterMode: 'all', feedIds: [],
        // channels
        toEmails: [], toInput: '',
        ccEmails: [], ccInput: '',
        bccEmails: [], bccInput: '',
        attachPdf: false,
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
          subTexts: (intent.sub_texts || []).map(st => ({
            language: st.language || 'en',
            text: st.text || '',
            translating: false,
          })),
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
          lookback_hours:   (s.lookback_seconds ?? 86400) / 3600,
          skip_seen:        s.skip_seen ?? true,
          history_days:     s.history_days != null ? String(s.history_days) : '',
          // event fields
          trigger_count:    s.trigger_count    ?? 3,
          max_wait_seconds: s.max_wait_seconds ?? 1800,
          // templates
          system_template:      intent.system_template      || 'default',
          instruction_template: intent.instruction_template || 'default',
          review_gate:          intent.review_gate         ?? false,
          // feed filter: null or {ids:null} → all (全扫); {ids:[...]} → specific
          feedFilterMode: (ff === null || ff === undefined || ff.ids === null || ff.ids === undefined) ? 'all' : 'specific',
          feedIds: ff?.ids ? [...ff.ids] : [],
          // channels
          toEmails: [...(ch.to  || [])], toInput: '',
          ccEmails: [...(ch.cc  || [])], ccInput: '',
          bccEmails: [...(ch.bcc || [])], bccInput: '',
          attachPdf: !!ch.attach_pdf,
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

    // ── Sub-texts (intent-match-enhancement) ──────────────
    // Slot identity is positional (D23): index 0 → alt_0, 1 → alt_1, 2 → alt_2.
    // Reordering with up/down would change slot identity and force a full re-embed,
    // so v1 UI deliberately omits a reorder control — add/edit/delete only.
    addSubText() {
      if (this.modal.form.subTexts.length >= 3) {
        this.showToast('Max 3 sub-texts', 'error');
        return;
      }
      this.modal.form.subTexts.push({ language: 'en', text: '', translating: false });
    },

    removeSubText(idx) {
      this.modal.form.subTexts.splice(idx, 1);
    },

    async translateSubText(idx) {
      // Cache form + sub references at call time. If the modal closes / reopens
      // (openCreate replaces this.modal wholesale) before the LLM responds,
      // we drop the response on the floor — writing data.text to a sub that
      // belongs to a discarded form would silently mutate stale state and
      // potentially leak across edit sessions. (Review loop 1 💡-2.)
      const form = this.modal.form;
      const sub = form.subTexts[idx];
      if (!sub) return;
      const source = (form.text || '').trim();
      if (!source) {
        this.showToast('Fill the main intent text before translating', 'error');
        return;
      }
      const target = (sub.language || '').trim();
      if (!target) {
        this.showToast('Pick a target language first', 'error');
        return;
      }
      if (sub.translating) return; // R11: in-flight guard, no debounce needed
      sub.translating = true;
      try {
        const res = await this._request('POST', '/intents/translate', {
          source_text: source,
          target_language: target,
        });
        if (this.modal.form !== form) return; // form discarded — abandon response
        if (res.ok) {
          const data = await res.json();
          sub.text = data.text || '';
        } else {
          const data = await res.json().catch(() => ({}));
          this.showToast('Translation failed: ' + this._extractError(data, `HTTP ${res.status}`), 'error');
        }
      } catch (e) {
        if (this.modal.form === form) {
          this.showToast('Network error: ' + e.message, 'error');
        }
      } finally {
        // Reset translating only if the form is still the live one (a discarded
        // form's sub is unreachable from UI anyway, but skipping the write avoids
        // ref-count noise on long-running translate retries).
        if (this.modal.form === form) {
          sub.translating = false;
        }
      }
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

      // intent-match-enhancement: mirror backend SubTextSpec validation (≤3 entries,
      // text 1..2000, language [A-Za-z][A-Za-z0-9_- ]{0..31}).
      if (f.subTexts.length > 3) {
        errors._global = (errors._global ? errors._global + '; ' : '') + 'Max 3 sub-texts';
      }
      for (let i = 0; i < f.subTexts.length; i++) {
        const st = f.subTexts[i];
        if (!st.text || !st.text.trim()) {
          errors['sub_text_' + i] = 'Required (or remove this entry)';
        } else if (st.text.length > 2000) {
          errors['sub_text_' + i] = 'Max 2000 characters';
        }
        if (!st.language || !st.language.trim()) {
          errors['sub_lang_' + i] = 'Required';
        } else if (st.language.length > 32) {
          errors['sub_lang_' + i] = 'Max 32 characters';
        } else if (!/^[A-Za-z][A-Za-z0-9_\- ]*$/.test(st.language)) {
          errors['sub_lang_' + i] = 'Must start with a letter; letters/digits/hyphens/underscores only';
        }
      }

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
        const lb = parseFloat(f.lookback_hours);
        if (isNaN(lb) || lb < 0.5 || lb > 720)
          errors.lookback_hours = 'Must be between 0.5 and 720 hours';
        if (f.history_days !== '') {
          const hdNum = Number(f.history_days);
          if (!Number.isInteger(hdNum) || hdNum < 1 || hdNum > 365)
            errors.history_days = 'Must be a whole number between 1 and 365';
        }
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
          lookback_seconds: Math.round(parseFloat(f.lookback_hours) * 3600),
          skip_seen:        !!f.skip_seen,
          history_days:     f.history_days !== '' ? Number(f.history_days) : null,
        }
        : {
          mode:             'event',
          trigger_count:    parseInt(f.trigger_count)    || 3,
          max_wait_seconds: parseInt(f.max_wait_seconds) || 1800,
        };

      const payload = {
        name:                 f.name.trim(),
        text:                 f.text.trim(),
        // intent-match-enhancement: PUT/POST body strips the transient `translating`
        // field; only language + text reach the backend.
        sub_texts: f.subTexts.map(st => ({
          language: (st.language || '').trim(),
          text: (st.text || '').trim(),
        })),
        threshold:            parseFloat(f.threshold),
        enabled:              !!f.enabled,
        language:             f.language.trim(),
        timezone:             f.timezone.trim(),
        tags:                 f.tags,
        schedule,
        system_template:      f.system_template      || 'default',
        instruction_template: f.instruction_template || 'default',
        review_gate:          !!f.review_gate,
        feed_filter:          f.feedFilterMode === 'all' ? null : { ids: f.feedIds },
        channels: [{
          type: 'email',
          to:   f.toEmails,
          cc:   f.ccEmails,
          bcc:  f.bccEmails,
          attach_pdf: f.attachPdf,
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
          lookback:  (s.lookback_seconds ?? 86400) / 3600,
          skip_seen: s.skip_seen        ?? true,
          threshold: intent.threshold   ?? 0.75,
          persist:   false,
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
      const lbHours = parseFloat(form.lookback);
      if (isNaN(lbHours) || lbHours < 0.5 || lbHours > 720) {
        this.fire.error = 'Lookback must be between 0.5 and 720 hours.'; return;
      }
      const lb = Math.round(lbHours * 3600);
      const thr = parseFloat(form.threshold);
      if (isNaN(thr) || thr < 0.20 || thr > 0.95) {
        this.fire.error = 'Threshold must be between 0.20 and 0.95.'; return;
      }
      const qs = new URLSearchParams({
        lookback:  lb,
        skip_seen: form.skip_seen,
        threshold: thr,
        persist:   form.persist,
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

    // ── History expand-row ─────────────────────────────────
    async openHistory(intent) {
      // Toggle off if clicking the already-open intent's button
      if (this.expandedIntentId === intent.id && this.expanded.kind === 'history') {
        this.closeHistory();
        return;
      }
      this.expandedIntentId = intent.id;
      this.expanded = {
        kind: 'history', loading: true, rows: [],
        limit: 100, offset: 0, hasMore: false, error: null,
      };
      await this._loadHistoryPage(intent.id);
    },

    closeHistory() {
      this.expandedIntentId = null;
      this.expanded = {
        kind: null, loading: false, rows: [],
        limit: 100, offset: 0, hasMore: false, error: null,
      };
    },

    async _loadHistoryPage(intentId) {
      try {
        const res = await this._request(
          'GET',
          `/intents/${intentId}/history?limit=${this.expanded.limit}&offset=${this.expanded.offset}`
        );
        if (!res.ok) {
          this.expanded.loading = false;
          this.expanded.error = `HTTP ${res.status}`;
          return;
        }
        const data = await res.json();
        this.expanded.rows = this.expanded.rows.concat(data.rows || []);
        this.expanded.hasMore = (data.rows || []).length === this.expanded.limit;
        this.expanded.loading = false;
      } catch (e) {
        this.expanded.loading = false;
        this.expanded.error = 'Network error: ' + e.message;
      }
    },

    async loadMoreHistory() {
      if (this.expanded.loading || !this.expanded.hasMore) return;
      this.expanded.loading = true;
      this.expanded.offset += this.expanded.limit;
      await this._loadHistoryPage(this.expandedIntentId);
    },

    historySnippet(row) {
      const s = row?.summary || '';
      return s.length > 120 ? s.slice(0, 120) + '…' : s;
    },

    // Format a summary_history.run_at (UTC ISO string) in the intent's
    // configured timezone as "M/D H:MM" — no year, no timezone label.
    // Used by the History expand-row table, the View modal title, and the
    // Delete-confirm modal so the user sees the same wall-clock the cron
    // would have fired at, not raw UTC.
    fmtHistoryRunAt(runAtIso, tz) {
      if (!runAtIso) return '';
      const d = new Date(runAtIso);
      if (isNaN(d.getTime())) return runAtIso;
      try {
        const parts = new Intl.DateTimeFormat('en-US', {
          timeZone: tz || 'UTC',
          month: 'numeric',
          day: 'numeric',
          hour: 'numeric',
          minute: '2-digit',
          hour12: false,
        }).formatToParts(d);
        const m = {};
        for (const p of parts) m[p.type] = p.value;
        // hour=24 occasionally appears on midnight boundary in some impls;
        // collapse to 0 so display reads naturally as "5/26 0:00".
        const hour = m.hour === '24' ? '0' : m.hour;
        return `${m.month}/${m.day} ${hour}:${m.minute}`;
      } catch (_) {
        return runAtIso;
      }
    },

    // ── View modal (single-row history detail with markdown render) ──
    openHistoryView(row, intent) {
      // Render markdown via vendored marked + DOMPurify. If either is missing
      // (e.g. CDN load failure), fall back to escaped pre-text so the modal
      // still displays something useful.
      let html;
      try {
        if (window.marked && window.DOMPurify) {
          const raw = window.marked.parse(row.summary || '', { breaks: true, gfm: true });
          html = window.DOMPurify.sanitize(raw);
        } else {
          html = '<pre style="white-space:pre-wrap;">' +
            (row.summary || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])) +
            '</pre>';
        }
      } catch (e) {
        html = '<pre>Render error: ' + e.message + '</pre>';
      }
      const citations = (row.citations || []).map(c => ({
        ...c,
        body: null,
        bodyOpen: false,
        bodyLoading: false,
        bodyError: null,
      }));
      this.historyView = {
        open: true,
        row,
        timezone: intent?.timezone || 'UTC',
        summaryHtml: html,
        citations,
      };
    },

    closeHistoryView() {
      this.historyView = {
        open: false,
        row: null,
        timezone: 'UTC',
        summaryHtml: '',
        citations: [],
      };
    },

    async loadCitationBody(citation) {
      if (citation.body !== null || citation.bodyLoading) {
        citation.bodyOpen = !citation.bodyOpen;
        return;
      }
      citation.bodyLoading = true;
      citation.bodyError = null;
      citation.bodyOpen = true;
      // article_id from summary_history.citations is a UUID-formatted string
      // (8-4-4-4-12 hex); the dashboard articles endpoint expects the raw
      // MD5 (32-char hex). Strip the dashes.
      const md5 = String(citation.article_id || '').replace(/-/g, '');
      try {
        const res = await this._request(
          'GET',
          `/api/dashboard/articles/${md5}?bucket=qdrant`
        );
        if (!res.ok) {
          citation.bodyError = `HTTP ${res.status}`;
          citation.bodyLoading = false;
          return;
        }
        const data = await res.json();
        citation.body = data.body || '(empty body)';
        citation.bodyLoading = false;
      } catch (e) {
        citation.bodyError = 'Network error: ' + e.message;
        citation.bodyLoading = false;
      }
    },

    // ── Delete row ────────────────────────────────────────
    confirmDeleteHistory(row, intent) {
      this.delHistory = { open: true, row, timezone: intent?.timezone || 'UTC' };
    },

    closeDeleteHistory() {
      this.delHistory = { open: false, row: null, timezone: 'UTC' };
    },

    async deleteHistoryRow() {
      const row = this.delHistory.row;
      if (!row) return;
      try {
        const res = await this._request(
          'DELETE',
          `/intents/${row.intent_id}/history/${row.id}`
        );
        if (!res.ok) {
          this.showToast(`Delete failed: HTTP ${res.status}`, 'error');
          return;
        }
        // Optimistic: drop the row from the local rows array.
        this.expanded.rows = this.expanded.rows.filter(r => r.id !== row.id);
        this.showToast('History row deleted', 'info');
        this.closeDeleteHistory();
      } catch (e) {
        this.showToast('Network error: ' + e.message, 'error');
      }
    },

    // ── Review gate ────────────────────────────────────────
    openReviewConfirm(row, intent) {
      this.review = {
        open: true,
        running: false,
        row,
        timezone: intent?.timezone || 'UTC',
        error: null,
      };
    },

    closeReview() {
      this.review = {
        open: false, running: false, row: null,
        timezone: 'UTC', error: null,
      };
    },

    async runReview() {
      const row = this.review.row;
      if (!row) return;
      this.review.running = true;
      this.review.error = null;
      try {
        const res = await this._request(
          'POST',
          `/intents/${row.intent_id}/history/${row.id}/review`
        );
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          const code = data?.detail?.code;  // object-detail from 422 responses
          if (code === 'source_articles_expired') {
            this.showToast('Source articles are no longer available in Qdrant; cannot review', 'error');
          } else if (code === 'digest_too_long') {
            this.showToast('Digest is too long to review (exceeds LLM token budget)', 'error');
          } else if (code === 'empty_summary') {
            this.showToast('Digest is empty, nothing to review', 'info');
          } else if (code === 'no_citations') {
            this.showToast('No citations to check against', 'info');
          } else {
            this.showToast('Review failed: ' + this._extractError(data, 'HTTP ' + res.status), 'error');
          }
          this.review.running = false;
          return;
        }
        const data = await res.json();
        this.closeReview();

        // D8: 0 corrections → just toast, don't open comparison modal
        if (!data.corrections || data.corrections.length === 0) {
          this.showToast('Review passed — no issues found', 'info');
          return;
        }

        // Render diff-highlighted markdown for old/new comparison
        const originalHtml = this._renderDiffMarkdown(data.original, data.corrections, 'original');
        const correctedHtml = this._renderDiffMarkdown(data.corrected, data.corrections, 'corrected');
        this.reviewCompare = {
          open: true,
          row,
          originalHtml,
          correctedHtml,
          correctedRaw: data.corrected,
          corrections: data.corrections,
          applying: false,
          error: null,
        };
      } catch (e) {
        this.review.error = 'Network error: ' + e.message;
        this.review.running = false;
      }
    },

    closeReviewCompare() {
      this.reviewCompare = {
        open: false, row: null,
        originalHtml: '', correctedHtml: '', correctedRaw: '',
        corrections: [], applying: false, error: null,
      };
    },

    async applyReview() {
      const row = this.reviewCompare.row;
      if (!row) return;
      this.reviewCompare.applying = true;
      this.reviewCompare.error = null;
      try {
        // PATCH the history row with the corrected summary (raw markdown stored from runReview)
        const patchRes = await this._request(
          'PATCH',
          `/intents/${row.intent_id}/history/${row.id}`,
          { summary: this.reviewCompare.correctedRaw }
        );
        if (!patchRes.ok) {
          this.reviewCompare.error = `Failed to save: HTTP ${patchRes.status}`;
          this.reviewCompare.applying = false;
          return;
        }
        // D4: refresh the full history list so the row shows the corrected text
        this.showToast('Corrections applied', 'info');
        this.closeReviewCompare();
        // Refresh the history rows inline (D4: full list re-fetch)
        const intentId = row.intent_id;
        if (this.expanded.intentId === intentId) {
          this.expanded.offset = 0;
          this.expanded.rows = [];
          this.expanded.loading = true;
          try {
            await this._loadHistoryPage(intentId);
          } catch (_) { /* best-effort refresh */ }
          this.expanded.loading = false;
        }
      } catch (e) {
        this.reviewCompare.error = 'Network error: ' + e.message;
        this.reviewCompare.applying = false;
      }
    },

    // Helper: render markdown with diff highlights injected before rendering.
    // Walks the corrections list and wraps each before/after substring in a
    // coloured span so the user sees exactly what changed at a glance.
    _renderDiffMarkdown(rawText, corrections, side) {
      let text = rawText;
      for (const c of (corrections || [])) {
        const needle = side === 'original' ? c.before : c.after;
        if (!needle) continue;
        const cls = side === 'original' ? 'diff-del' : 'diff-ins';
        // Escape regex-special chars in the literal needle.
        const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        text = text.replace(new RegExp(escaped, 'g'), '<span class="' + cls + '">' + needle + '</span>');
      }
      return this._renderMarkdown(text);
    },

    // Helper: plain markdown render (used by openHistoryView and as fallback).
    _renderMarkdown(text) {
      try {
        if (window.marked && window.DOMPurify) {
          const raw = window.marked.parse(text || '', { breaks: true, gfm: true });
          return window.DOMPurify.sanitize(raw);
        }
      } catch (e) { /* fall through */ }
      return '<pre style="white-space:pre-wrap;">' +
        (text || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])) +
        '</pre>';
    },

    // Sync-scroll guard: prevent scroll event loops when one panel's
    // programmatic scrollTop triggers the other's @scroll handler.
    syncDiffScroll(from) {
      if (this._scrollLock) return;
      this._scrollLock = true;
      const orig = this.$refs.diffScrollOrig;
      const corr = this.$refs.diffScrollCorr;
      if (orig && corr) {
        if (from === 'orig') corr.scrollTop = orig.scrollTop;
        else orig.scrollTop = corr.scrollTop;
      }
      setTimeout(() => { this._scrollLock = false; }, 30);
    },

    // ── Backfill modal ─────────────────────────────────────
    openBackfill(intent) {
      if (this.backfill._timer) clearTimeout(this.backfill._timer);
      this.backfill = {
        open: true, phase: 'form', intent,
        form: { past_runs: 7 },
        taskId: null, statusUrl: null, progress: null,
        error: null, depthError: null, _timer: null,
      };
    },

    closeBackfill() {
      if (this.backfill._timer) clearTimeout(this.backfill._timer);
      this.backfill.open = false;
    },

    async runBackfill() {
      const { intent, form } = this.backfill;
      this.backfill.error = null;
      this.backfill.depthError = null;
      const n = parseInt(form.past_runs, 10);
      if (isNaN(n) || n < 1 || n > 365) {
        this.backfill.error = 'past_runs must be between 1 and 365.';
        return;
      }
      this.backfill.phase = 'running';
      try {
        const res = await this._request(
          'POST', `/intents/${intent.id}/backfill`, { past_runs: n }
        );
        if (res.status === 409) {
          this.backfill.phase = 'form';
          const data = await res.json().catch(() => ({}));
          const d = data.detail || '';
          if (d === 'backfill_in_progress') {
            this.backfill.error = 'A backfill is already running for this intent.';
          } else {
            this.backfill.error = d || 'Conflict.';
          }
          return;
        }
        if (res.status === 422) {
          this.backfill.phase = 'form';
          const data = await res.json().catch(() => ({}));
          // 422 from depth-check is { detail: { code, oldest_date, max_backfillable_runs } }
          // 422 from Pydantic body validation is { detail: [{loc, msg}, ...] }
          if (data?.detail?.code === 'qdrant_depth_insufficient') {
            this.backfill.depthError = {
              oldest_date: data.detail.oldest_date,
              max_backfillable_runs: data.detail.max_backfillable_runs,
            };
          } else {
            this.backfill.error = this._extractError(data, 'Validation error');
          }
          return;
        }
        if (!res.ok) {
          this.backfill.phase = 'form';
          const data = await res.json().catch(() => ({}));
          this.backfill.error = this._extractError(data, `HTTP ${res.status}`);
          return;
        }
        const data = await res.json();
        this.backfill.taskId    = data.task_id;
        this.backfill.statusUrl = data.status_url;
        this._pollBackfill(0);
      } catch (e) {
        this.backfill.phase = 'form';
        this.backfill.error = 'Network error: ' + e.message;
      }
    },

    _pollBackfill(elapsed) {
      // Cap polling at 10 minutes — backfill jobs can outlive that, but the
      // modal stops asking. The task keeps running server-side; user can
      // close the browser and the in-memory BackfillTask survives until the
      // 24h sweep clears it.
      if (elapsed >= 600000) {
        this.backfill.phase  = 'result';
        this.backfill.error  = 'Polling timed out after 10 minutes. '
          + 'The backfill continues in the background; refresh History to see new rows.';
        return;
      }
      this.backfill._timer = setTimeout(async () => {
        try {
          const res = await this._request('GET', this.backfill.statusUrl);
          if (!res.ok) {
            this.backfill.phase = 'result';
            this.backfill.error = `Poll error: HTTP ${res.status}`;
            return;
          }
          const data = await res.json();
          this.backfill.progress = data.progress;
          if (data.status === 'done' || data.status === 'error') {
            this.backfill.phase  = 'result';
            if (data.status === 'error') {
              this.backfill.error = 'Backfill failed: ' + (data.error_reason || 'unknown');
            } else {
              // Refresh history rows so user sees the new entries inline.
              if (this.expandedIntentId === this.backfill.intent.id) {
                this.expanded.offset = 0;
                this.expanded.rows = [];
                this._loadHistoryPage(this.backfill.intent.id);
              }
            }
          } else {
            this._pollBackfill(elapsed + 2000);
          }
        } catch (e) {
          this.backfill.phase = 'result';
          this.backfill.error = 'Network error during poll: ' + e.message;
        }
      }, 2000);
    },

    // ── Summarize modal ────────────────────────────────────

    _defaultAggregatePrompt(language) {
      if (language === 'zh') {
        return [
          '请根据以下每日摘要记录，生成一份结构化的阶段性回顾报告。要求：',
          '',
          '## 1. 时间线回顾',
          '按时间顺序梳理关键事件与发展脉络，标注重要的变化节点。',
          '',
          '## 2. 重点关注',
          '列出本期值得持续跟踪的主题、实体或趋势，简要说明原因。',
          '',
          '## 3. 前瞻推演',
          '基于当前态势，推演未来可能出现的 2-3 个情景：',
          '- 基准情景（最可能）',
          '- 乐观/上行情景',
          '- 悲观/下行情景',
          '每个情景标注概率评估（%）与关键触发条件。',
          '',
          '格式使用 Markdown，语言为中文。',
          '---',
          '{history}',
        ].join('\n');
      }
      return [
        'Based on the daily digest records below, produce a structured periodic review report. Requirements:',
        '',
        '## 1. Timeline Review',
        'Trace key events and developments in chronological order, highlighting notable shifts.',
        '',
        '## 2. Key Focus Areas',
        'Identify themes, entities, or trends worth continued attention during this period, with brief reasons.',
        '',
        '## 3. Forward Look',
        'Project 2–3 plausible scenarios based on the current trajectory:',
        '- Baseline scenario (most likely)',
        '- Upside / optimistic scenario',
        '- Downside / pessimistic scenario',
        'Annotate each with a probability estimate (%) and key trigger conditions.',
        '',
        'Use Markdown formatting.',
        '---',
        '{history}',
      ].join('\n');
    },

    _todayStr() {
      return new Date().toISOString().slice(0, 10);
    },

    _daysAgoStr(n) {
      const d = new Date();
      d.setDate(d.getDate() - n);
      return d.toISOString().slice(0, 10);
    },

    openSummarize(intent) {
      const until = this._todayStr();
      const since = this._daysAgoStr(6);
      const gen = (this.summarize._gen || 0) + 1;
      this.summarize = {
        open: true,
        intent,
        loading: false,
        form: { since, until, prompt: this._defaultAggregatePrompt(intent.language) },
        result: null,
        error: null,
        cachedSinceUntil: null,
        sendLoading: false,
        _gen: gen,
      };
    },

    closeSummarize() {
      this.summarize.open = false;
    },

    async runSummarize() {
      const { intent, form } = this.summarize;
      const gen = this.summarize._gen;  // capture generation at call time
      this.summarize.error = null;
      this.summarize.result = null;
      const since = form.since.trim();
      const until = form.until.trim();
      if (!since || !until) {
        this.summarize.error = 'Both since and until dates are required.';
        return;
      }
      this.summarize.loading = true;
      try {
        const res = await this._request(
          'POST', `/intents/${intent.id}/history/aggregate`,
          { since, until, prompt: form.prompt }
        );
        if (this.summarize._gen !== gen) return;  // modal closed + reopened — discard
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          this.summarize.error = this._extractError(data, `HTTP ${res.status}`);
          return;
        }
        this.summarize.result = data;
        this.summarize.cachedSinceUntil = { since, until };
      } catch (e) {
        if (this.summarize._gen !== gen) return;
        this.summarize.error = 'Network error: ' + e.message;
      } finally {
        if (this.summarize._gen === gen) this.summarize.loading = false;
      }
    },

    async sendSummarize() {
      const { intent, cachedSinceUntil, result } = this.summarize;
      const gen = this.summarize._gen;
      if (!cachedSinceUntil || !result?.summary) {
        this.showToast('No summary to send. Run Summarize first.', 'error');
        return;
      }
      this.summarize.sendLoading = true;
      try {
        const res = await this._request(
          'POST', `/intents/${intent.id}/history/aggregate/send`,
          { since: cachedSinceUntil.since, until: cachedSinceUntil.until, markdown: result.summary }
        );
        if (this.summarize._gen !== gen) return;
        const data = await res.json().catch(() => ({}));
        const results = data.results || [];
        const failed = results.filter(r => !r.ok);
        if (failed.length === 0) {
          this.showToast('Email sent.', 'info');
        } else if (failed.length === results.length) {
          this.showToast('Send failed: ' + failed.map(r => r.error).join('; '), 'error');
        } else {
          this.showToast('Partially sent: ' + failed.length + '/' + results.length + ' failed.', 'error');
        }
      } catch (e) {
        if (this.summarize._gen !== gen) return;
        this.showToast('Network error: ' + e.message, 'error');
      } finally {
        if (this.summarize._gen === gen) this.summarize.sendLoading = false;
      }
    },

    copySummary() {
      const md = this.summarize.result?.summary;
      if (!md) return;
      // Try Clipboard API first (requires HTTPS or localhost), fall back to
      // execCommand for plain-HTTP deployments (e.g. internal dashboard).
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(md).then(
          () => this.showToast('Summary copied to clipboard.', 'info'),
          () => this._fallbackCopy(md),
        );
      } else {
        this._fallbackCopy(md);
      }
    },

    _fallbackCopy(text) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        this.showToast('Summary copied to clipboard.', 'info');
      } catch (_) {
        this.showToast('Failed to copy.', 'error');
      } finally {
        document.body.removeChild(ta);
      }
    },

    downloadSummary() {
      const md = this.summarize.result?.summary;
      if (!md) return;
      const blob = new Blob([md], { type: 'text/markdown' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `summary-intent-${this.summarize.intent.id}.md`;
      a.click();
      URL.revokeObjectURL(url);
    },

    // ── Export modal ────────────────────────────────────────

    openExport(intent) {
      const until = this._todayStr();
      const since = this._daysAgoStr(6);
      const gen = (this.export_._gen || 0) + 1;
      this.export_ = {
        open: true,
        intent,
        loading: false,
        form: { since, until },
        error: null,
        _gen: gen,
      };
    },

    closeExport() {
      this.export_.open = false;
    },

    async runExport() {
      const { intent, form } = this.export_;
      const gen = this.export_._gen;
      this.export_.error = null;
      const since = form.since.trim();
      const until = form.until.trim();
      if (!since || !until) {
        this.export_.error = 'Both since and until dates are required.';
        return;
      }
      this.export_.loading = true;
      try {
        const res = await this._request(
          'GET',
          `/intents/${intent.id}/history/export?since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`
        );
        if (this.export_._gen !== gen) return;  // modal closed + reopened — discard
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          this.export_.error = this._extractError(data, `HTTP ${res.status}`);
          return;
        }
        const blob = await res.blob();
        const text = await blob.text();
        let rows;
        try { rows = JSON.parse(text); } catch (_) { rows = null; }
        if (Array.isArray(rows) && rows.length === 0) {
          this.showToast('No history rows in this date range.', 'info');
          return;
        }
        const disposition = res.headers.get('content-disposition') || '';
        const match = disposition.match(/filename=(.+)/);
        const filename = match ? match[1] : `intent-${intent.id}-${since}-${until}.json`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
        this.showToast('Export downloaded.', 'info');
      } catch (e) {
        if (this.export_._gen !== gen) return;
        this.export_.error = 'Network error: ' + e.message;
      } finally {
        if (this.export_._gen === gen) this.export_.loading = false;
      }
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
