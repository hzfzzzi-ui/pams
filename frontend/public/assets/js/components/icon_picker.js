// Unified icon-triggered glass popover pickers for time ranges, periods, and sort controls.
// Pure presentation adapter: proxies selections back to the original native <select> or button group.

const ICONS = {
  calendar: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4m8-4v4M3 10h18m-13 5h3m3 0h2"/></svg>',
  sort: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4v16m0 0-3-3m3 3 3-3M17 20V4m0 0-3 3m3-3 3 3"/></svg>',
  filter: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M7 12h10M10 17h4"/></svg>',
};

const CHECK_SVG = '<svg class="pamsIconCheck" viewBox="0 0 16 16" aria-hidden="true"><path d="m3 8.2 3.1 3.1L13 4.5"/></svg>';

const PICKER_RULES = [
  { selector: '#monthlyOverview header select', kind: 'select', icon: 'calendar', title: '选择月份' },
  { selector: '#holdingSortOrder', kind: 'select', icon: 'sort', title: '排序方式' },
  { selector: '#investmentReturnWindowToggle', kind: 'buttons', icon: 'calendar', title: '时间区间' },
  { selector: '#investmentNavWindowToggle', kind: 'buttons', icon: 'calendar', title: '时间区间' },
  { selector: '.accountHistoryHead .returnWindowSegment', kind: 'buttons', icon: 'calendar', title: '时间区间' },
  { selector: '.ledgerOverviewPeriod .ledgerTextSwitch', kind: 'buttons', icon: 'calendar', title: '统计周期' },
  { selector: '.ledgerTrendCard .ledgerTextSwitch', kind: 'buttons', icon: 'calendar', title: '趋势粒度' },
  { selector: '.categoryInsightPanel .ledgerTextSwitch', kind: 'buttons', icon: 'filter', title: '分类视图' },
];

export function mountUnifiedIconPickers(doc = document, win = window) {
  const entries = new Map();
  const ac = new AbortController();
  let disposed = false;
  let scanQueued = false;

  function closeAll(except = null) {
    for (const entry of entries.values()) {
      if (entry === except) continue;
      entry.menu.hidden = true;
      entry.trigger.setAttribute('aria-expanded', 'false');
    }
  }

  function readOptions(el, kind) {
    if (kind === 'select') {
      return [...el.options].map((opt, index) => ({
        index,
        label: opt.textContent.trim(),
        active: index === el.selectedIndex,
        disabled: opt.disabled,
      }));
    }
    return [...el.querySelectorAll('button')].map((btn, index) => ({
      index,
      label: btn.textContent.trim(),
      active: btn.classList.contains('active') || btn.getAttribute('aria-pressed') === 'true',
      disabled: btn.disabled,
      button: btn,
    }));
  }

  function syncEntry(entry) {
    const { el, kind, wrap, trigger, labelEl, menu } = entry;
    const options = readOptions(el, kind);
    const shouldHide = el.hidden || options.length === 0;
    wrap.hidden = shouldHide;
    if (shouldHide) {
      menu.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
      return;
    }
    const active = options.find(opt => opt.active) || options[0];
    labelEl.textContent = active ? active.label : '';
    trigger.title = `${entry.title}${active ? `：${active.label}` : ''}`;
    trigger.setAttribute('aria-label', trigger.title);

    menu.innerHTML = '';
    const head = doc.createElement('div');
    head.className = 'pamsIconMenuHead';
    head.textContent = entry.title;
    menu.append(head);

    for (const opt of options) {
      const item = doc.createElement('button');
      item.type = 'button';
      item.className = `pamsIconMenuItem${opt.active ? ' is-active' : ''}`;
      item.disabled = Boolean(opt.disabled);
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('aria-checked', opt.active ? 'true' : 'false');
      const text = doc.createElement('span');
      text.textContent = opt.label;
      item.append(text);
      if (opt.active) item.insertAdjacentHTML('beforeend', CHECK_SVG);
      item.addEventListener('click', () => {
        if (kind === 'select') {
          if (el.selectedIndex !== opt.index) {
            el.selectedIndex = opt.index;
            el.dispatchEvent(new Event('change', { bubbles: true }));
          }
        } else if (opt.button) {
          opt.button.click();
        }
        menu.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
        win.setTimeout(() => { if (!disposed) syncEntry(entry); }, 0);
      });
      menu.append(item);
    }
  }

  function enhance(el, rule) {
    if (!el || entries.has(el) || el.dataset.pamsIconPicker === '1') return;
    el.dataset.pamsIconPicker = '1';
    el.classList.add('pamsPickerOriginHidden');

    const wrap = doc.createElement('div');
    wrap.className = 'pamsIconPicker';

    const trigger = doc.createElement('button');
    trigger.type = 'button';
    trigger.className = 'pamsIconTrigger';
    trigger.setAttribute('aria-haspopup', 'menu');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.innerHTML = ICONS[rule.icon] || ICONS.calendar;

    const labelEl = doc.createElement('span');
    labelEl.className = 'pamsIconTriggerLabel';
    trigger.append(labelEl);

    const menu = doc.createElement('div');
    menu.className = 'pamsIconMenu';
    menu.setAttribute('role', 'menu');
    menu.hidden = true;

    wrap.append(trigger, menu);
    el.insertAdjacentElement('afterend', wrap);

    const entry = { el, kind: rule.kind, title: rule.title, wrap, trigger, labelEl, menu, observer: null };
    entries.set(el, entry);

    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      const willOpen = menu.hidden;
      closeAll(entry);
      if (willOpen) syncEntry(entry);
      menu.hidden = !willOpen;
      trigger.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    }, { signal: ac.signal });

    el.addEventListener('change', () => syncEntry(entry), { signal: ac.signal });
    el.addEventListener('click', () => win.setTimeout(() => { if (!disposed) syncEntry(entry); }, 0), { signal: ac.signal });

    if (typeof MutationObserver === 'function') {
      const obs = new MutationObserver(() => {
        if (!disposed) syncEntry(entry);
      });
      obs.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'class', 'aria-pressed', 'disabled'] });
      entry.observer = obs;
    }

    syncEntry(entry);
  }

  function scan() {
    scanQueued = false;
    if (disposed) return;
    for (const rule of PICKER_RULES) {
      doc.querySelectorAll(rule.selector).forEach(el => enhance(el, rule));
    }
    for (const [el, entry] of [...entries]) {
      if (!el.isConnected) {
        entry.observer?.disconnect();
        entry.wrap.remove();
        entries.delete(el);
      }
    }
  }

  function queueScan() {
    if (scanQueued || disposed) return;
    scanQueued = true;
    Promise.resolve().then(scan);
  }

  doc.addEventListener('click', (event) => {
    for (const entry of entries.values()) {
      if (!entry.menu.hidden && !entry.wrap.contains(event.target)) {
        entry.menu.hidden = true;
        entry.trigger.setAttribute('aria-expanded', 'false');
      }
    }
  }, { signal: ac.signal });

  doc.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeAll();
  }, { signal: ac.signal });

  let bodyObserver = null;
  if (typeof MutationObserver === 'function' && doc.body) {
    bodyObserver = new MutationObserver(queueScan);
    bodyObserver.observe(doc.body, { childList: true, subtree: true });
  }

  scan();

  return function dispose() {
    if (disposed) return;
    disposed = true;
    ac.abort();
    bodyObserver?.disconnect();
    for (const [el, entry] of entries) {
      entry.observer?.disconnect();
      entry.wrap.remove();
      el.classList.remove('pamsPickerOriginHidden');
      delete el.dataset.pamsIconPicker;
    }
    entries.clear();
  };
}
