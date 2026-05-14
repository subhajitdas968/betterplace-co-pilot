/* Placeholder autocomplete widget.
 *
 * Wire any textarea or text input with class `ph-autocomplete` and the
 * caret-typed sequence `{{` or `<` opens a searchable popup of every
 * placeholder defined by /api/placeholders/catalog. Selecting one inserts
 * `{{ticket.foo.bar}}` at the cursor.
 *
 * Used by:
 *   - automation action params (textarea/text in the rule builder modal)
 *   - reply box (textarea#reply-text) — for use in templates
 *   - auto-reply admin (body textarea)
 *   - SLA / business-hours admin etc.
 *
 * Usage:
 *   <textarea class="ph-autocomplete">...</textarea>
 *   <script src="/static/placeholder-autocomplete.js"></script>
 *
 * Or programmatically: PlaceholderAutocomplete.attach(elementOrSelector);
 */

(function () {
  let CATALOG = null;
  let CATALOG_PROMISE = null;
  let popup = null;
  let popupItems = [];
  let popupActiveIdx = 0;
  let popupTarget = null;       // textarea/input the popup is attached to
  let popupTriggerStart = 0;    // caret position where {{ or < started
  let popupTriggerChar = '';    // '{' or '<'

  // -----------------------------------------------------------------
  // Fetch (and cache) the catalog
  // -----------------------------------------------------------------
  function loadCatalog() {
    if (CATALOG) return Promise.resolve(CATALOG);
    if (CATALOG_PROMISE) return CATALOG_PROMISE;
    CATALOG_PROMISE = fetch('/api/placeholders/catalog')
      .then(r => r.ok ? r.json() : {items: []})
      .then(out => { CATALOG = out.items || []; return CATALOG; })
      .catch(() => { CATALOG = []; return CATALOG; });
    return CATALOG_PROMISE;
  }

  // -----------------------------------------------------------------
  // Popup DOM
  // -----------------------------------------------------------------
  function ensurePopup() {
    if (popup) return popup;
    popup = document.createElement('div');
    popup.className = 'ph-popup';
    popup.style.cssText = [
      'position:absolute', 'z-index:9999', 'display:none',
      'background:#0f172a', 'color:#e2e8f0',
      'border:1px solid #475569', 'border-radius:8px',
      'box-shadow:0 10px 30px rgba(0,0,0,.45)',
      'max-width:420px', 'max-height:300px', 'overflow:auto',
      'font-family:Inter,system-ui,sans-serif',
    ].join(';');
    document.body.appendChild(popup);
    popup.addEventListener('mousedown', (ev) => {
      // mousedown so click happens before blur
      const li = ev.target.closest('.ph-item');
      if (!li) return;
      ev.preventDefault();
      pickIndex(parseInt(li.dataset.idx));
    });
    return popup;
  }

  function renderPopup(filter) {
    const q = (filter || '').toLowerCase();
    popupItems = (CATALOG || []).filter(p =>
      !q || p.key.toLowerCase().includes(q) || (p.label || '').toLowerCase().includes(q)
    );
    if (popupItems.length === 0) {
      popup.innerHTML = `<div style="padding:10px;color:#94a3b8;font-size:12px;">No placeholders match "${escapeHtml(q)}"</div>`;
      return;
    }
    // Group rendering
    const groups = {};
    popupItems.forEach((p, i) => {
      const g = p.group || 'Other';
      (groups[g] = groups[g] || []).push({...p, _idx: i});
    });
    let html = '';
    for (const g of Object.keys(groups)) {
      html += `<div style="padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;background:#1e293b;border-bottom:1px solid #334155;">${escapeHtml(g)}</div>`;
      for (const p of groups[g]) {
        const active = p._idx === popupActiveIdx;
        html += `<div class="ph-item" data-idx="${p._idx}" style="padding:6px 12px;cursor:pointer;border-bottom:1px solid #1e293b;background:${active ? '#312e81' : 'transparent'};">
          <div style="font-size:12.5px;color:${active ? '#fff' : '#e2e8f0'};font-family:'JetBrains Mono',monospace;">{{${escapeHtml(p.key)}}}</div>
          <div style="font-size:10.5px;color:${active ? '#c7d2fe' : '#94a3b8'};margin-top:1px;">${escapeHtml(p.label || '')}</div>
        </div>`;
      }
    }
    popup.innerHTML = html;
    // Scroll active into view
    const act = popup.querySelector('.ph-item[data-idx="' + popupActiveIdx + '"]');
    if (act) act.scrollIntoView({block: 'nearest'});
  }

  function positionPopupBelow(el) {
    const r = el.getBoundingClientRect();
    popup.style.left = (window.scrollX + r.left + 12) + 'px';
    popup.style.top  = (window.scrollY + r.bottom + 4) + 'px';
    popup.style.minWidth = Math.min(360, r.width) + 'px';
  }

  function hidePopup() {
    if (popup) popup.style.display = 'none';
    popupTarget = null;
    popupActiveIdx = 0;
  }

  function pickIndex(idx) {
    const item = popupItems[idx];
    if (!item || !popupTarget) return;
    const ta = popupTarget;
    const before = ta.value.slice(0, popupTriggerStart);
    const after  = ta.value.slice(ta.selectionStart);
    const insert = '{{' + item.key + '}}';
    ta.value = before + insert + after;
    const newCaret = before.length + insert.length;
    ta.selectionStart = ta.selectionEnd = newCaret;
    ta.dispatchEvent(new Event('input', {bubbles: true}));
    ta.focus();
    hidePopup();
  }

  // -----------------------------------------------------------------
  // Detect the trigger sequence in the textarea
  // -----------------------------------------------------------------
  function checkTriggerAt(el) {
    const pos = el.selectionStart;
    const before = el.value.slice(0, pos);
    // Look back to find the last `{{` or `<` and ensure no `}}` / `>` /
    // whitespace / newline appears between it and the caret.
    let i = pos - 1;
    let triggerStart = -1, triggerChar = '';
    while (i >= 0) {
      const ch = before[i];
      if (ch === '\n' || ch === ' ' || ch === '}' || ch === '>') break;
      // `{{` opens
      if (ch === '{' && i > 0 && before[i-1] === '{') {
        triggerStart = i - 1; triggerChar = '{'; break;
      }
      // `<` opens (single char)
      if (ch === '<') {
        triggerStart = i; triggerChar = '<'; break;
      }
      i -= 1;
    }
    if (triggerStart < 0) return null;
    // Don't trigger if it's the `<` of an HTML-looking tag
    if (triggerChar === '<') {
      const next = before[triggerStart + 1];
      if (next === '/' || next === '!' || /\s/.test(next || '')) return null;
    }
    const filterStart = triggerChar === '{' ? triggerStart + 2 : triggerStart + 1;
    const filter = before.slice(filterStart);
    return { triggerStart, triggerChar, filter };
  }

  // -----------------------------------------------------------------
  // Attach to one element
  // -----------------------------------------------------------------
  function attachOne(el) {
    if (el.__phAttached) return;
    el.__phAttached = true;

    function refresh() {
      const t = checkTriggerAt(el);
      if (!t) { hidePopup(); return; }
      loadCatalog().then(() => {
        ensurePopup();
        popupTarget = el;
        popupTriggerStart = t.triggerStart;
        popupTriggerChar = t.triggerChar;
        popupActiveIdx = 0;
        renderPopup(t.filter);
        positionPopupBelow(el);
        popup.style.display = 'block';
      });
    }

    el.addEventListener('input', refresh);
    el.addEventListener('click', refresh);
    el.addEventListener('focus', refresh);
    el.addEventListener('blur', () => setTimeout(hidePopup, 100));   // delay so item click registers
    el.addEventListener('keydown', (ev) => {
      if (!popup || popup.style.display === 'none' || popupTarget !== el) return;
      if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        popupActiveIdx = Math.min(popupItems.length - 1, popupActiveIdx + 1);
        renderPopup(checkTriggerAt(el)?.filter || '');
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault();
        popupActiveIdx = Math.max(0, popupActiveIdx - 1);
        renderPopup(checkTriggerAt(el)?.filter || '');
      } else if (ev.key === 'Enter' || ev.key === 'Tab') {
        ev.preventDefault();
        pickIndex(popupActiveIdx);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        hidePopup();
      }
    });
  }

  function attach(target) {
    if (!target) return;
    if (typeof target === 'string') {
      document.querySelectorAll(target).forEach(attachOne);
    } else if (target.nodeType === 1) {
      attachOne(target);
    } else if (target.length) {
      Array.from(target).forEach(attachOne);
    }
  }

  // Auto-attach on page load + observe DOM mutations
  function autoAttach() {
    document.querySelectorAll('textarea.ph-autocomplete, input.ph-autocomplete, [data-ph]').forEach(attachOne);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoAttach);
  } else {
    autoAttach();
  }

  const mo = new MutationObserver(() => autoAttach());
  mo.observe(document.body, {childList: true, subtree: true});

  // -----------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  window.PlaceholderAutocomplete = { attach, loadCatalog, hidePopup };
})();
