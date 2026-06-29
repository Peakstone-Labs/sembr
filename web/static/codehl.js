// Shared syntax-highlight helpers for the dashboard's overlay code editors:
// the spec-autogen Advanced panel (.json / .md) and the templates editor (.md).
// Always escape-first so any stray markup in user input is neutralized (XSS-safe);
// the highlight layer is display-only. Exposed on window so both Alpine
// components (intentsView, templatesTab) share one implementation.
(function () {
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  window.ceEscapeHtml = esc;

  window.ceHighlightJson = function (text) {
    const e = esc(text);
    return e.replace(
      /(&quot;(?:\\.|(?!&quot;)[\s\S])*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
      (full, str, colon, kw, num) => {
        if (str !== undefined) {
          const cls = colon ? 'json-key' : 'json-str';
          return `<span class="${cls}">${str}</span>` + (colon || '');
        }
        if (kw !== undefined) return `<span class="json-${kw === 'null' ? 'null' : 'bool'}">${kw}</span>`;
        return `<span class="json-num">${num}</span>`;
      }
    );
  };

  window.ceHighlightMarkdown = function (text) {
    return esc(text).split('\n').map(line => {
      const h = line.match(/^(#{1,6}\s.*)$/);
      if (h) return `<span class="md-h">${h[1]}</span>`;
      if (/^&gt;\s?/.test(line)) return `<span class="md-quote">${line}</span>`;
      const lm = line.match(/^(\s*)([-*+]|\d+\.)(\s.*)$/);
      let prefix = '', body = line;
      if (lm) { prefix = `${lm[1]}<span class="md-bullet">${lm[2]}</span>`; body = lm[3]; }
      body = body
        .replace(/(`[^`]+`)/g, '<span class="md-code">$1</span>')
        .replace(/(\*\*[^*]+\*\*)/g, '<span class="md-bold">$1</span>');
      return prefix + body;
    }).join('\n');
  };
})();
