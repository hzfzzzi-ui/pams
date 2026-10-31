const FOCUSABLE = 'a[href], area[href], button, input, select, textarea, [contenteditable="true"], [tabindex]';

export function createBottomSheet(doc = document) {
  const win = doc.defaultView;
  let sheet;
  let panel;
  let body;
  let opened = false;
  let disposed = false;
  let opener;
  let events;
  let observer;
  let inheritedScrollLock = false;
  const background = new Map();

  function create() {
    sheet = doc.createElement('div');
    sheet.id = 'appBottomSheet';
    sheet.className = 'appBottomSheet';
    sheet.hidden = true;
    sheet.innerHTML = `
      <div class="appBottomSheetScrim" data-sheet-close></div>
      <section class="appBottomSheetPanel" role="dialog" aria-modal="true" aria-labelledby="appBottomSheetTitle" tabindex="-1">
        <div class="appBottomSheetHandle" aria-hidden="true"></div>
        <div class="appBottomSheetHead">
          <h3 id="appBottomSheetTitle"></h3>
          <button type="button" class="appBottomSheetClose" data-sheet-close>关闭</button>
        </div>
        <div class="appBottomSheetBody" id="appBottomSheetBody"></div>
      </section>
    `;
    panel = sheet.querySelector('.appBottomSheetPanel');
    body = sheet.querySelector('#appBottomSheetBody');
    doc.body.append(sheet);
  }

  function focusables() {
    const nodes = [...panel.querySelectorAll(FOCUSABLE)].filter(node => (
      node.tabIndex >= 0 && !node.matches(':disabled') && !node.closest('[hidden], [inert]')
      && node.getClientRects().length > 0 && win.getComputedStyle(node).visibility === 'visible'
    ));
    return nodes.filter(node => {
      if (node.type !== 'radio' || !node.name) return true;
      const group = nodes.filter(other => other.type === 'radio' && other.name === node.name && other.form === node.form);
      return node === (group.find(other => other.checked) || group[0]);
    }).sort((a, b) => (a.tabIndex > 0 ? a.tabIndex : Infinity) - (b.tabIndex > 0 ? b.tabIndex : Infinity));
  }

  function focusFirst() {
    const items = focusables();
    const preferred = items.find(node => node.hasAttribute('autofocus'));
    (preferred || items[0] || panel).focus({preventScroll: true});
  }

  function lockBackground(node) {
    if (node === sheet || node.nodeType !== 1 || background.has(node)) return;
    background.set(node, {inert: node.inert, ariaHidden: node.getAttribute('aria-hidden')});
    node.inert = true;
    node.setAttribute('aria-hidden', 'true');
  }

  function unlockBackground() {
    for (const [node, before] of background) {
      node.inert = before.inert;
      if (before.ariaHidden === null) node.removeAttribute('aria-hidden');
      else node.setAttribute('aria-hidden', before.ariaHidden);
    }
    background.clear();
  }

  function onKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const items = focusables();
    if (!items.length) {
      event.preventDefault();
      panel.focus({preventScroll: true});
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const current = doc.activeElement;
    if (event.shiftKey && (current === first || current === panel || !panel.contains(current))) {
      event.preventDefault();
      last.focus({preventScroll: true});
    } else if (!event.shiftKey && (current === last || !panel.contains(current))) {
      event.preventDefault();
      first.focus({preventScroll: true});
    }
  }

  function activate() {
    events = new win.AbortController();
    const {signal} = events;
    doc.addEventListener('keydown', onKeydown, {capture: true, signal});
    doc.addEventListener('focusin', event => {
      if (opened && !panel.contains(event.target)) focusFirst();
    }, {signal});
    sheet.addEventListener('click', event => {
      if (event.target?.closest?.('[data-sheet-close]')) close();
    }, {signal});
    win.addEventListener('pagehide', () => dispose({restoreFocus: false}), {once: true, signal});
    for (const node of doc.body.children) lockBackground(node);
    observer = new win.MutationObserver(records => {
      for (const record of records) for (const node of record.addedNodes) lockBackground(node);
    });
    observer.observe(doc.body, {childList: true});
  }

  function open(title, html) {
    if (disposed) throw new Error('Cannot reopen a disposed bottom sheet controller');
    if (!sheet) create();
    const wasOpen = opened;
    if (!wasOpen) {
      opener = doc.activeElement;
      inheritedScrollLock = doc.body.classList.contains('sheet-open');
    }
    sheet.querySelector('#appBottomSheetTitle').textContent = title || '详情';
    body.innerHTML = html || '';
    sheet.hidden = false;
    sheet.classList.add('open');
    doc.body.classList.add('sheet-open');
    opened = true;
    // Move focus before hiding the previous opener from the accessibility tree.
    focusFirst();
    if (!wasOpen) activate();
    return body;
  }

  function close({restoreFocus = true} = {}) {
    if (!opened) return;
    opened = false;
    events?.abort();
    events = null;
    observer?.disconnect();
    observer = null;
    unlockBackground();
    sheet.classList.remove('open');
    sheet.hidden = true;
    if (!inheritedScrollLock) doc.body.classList.remove('sheet-open');
    body.replaceChildren();
    if (restoreFocus) {
      if (opener?.isConnected && !opener.closest('[inert]') && opener.getClientRects().length) {
        opener.focus({preventScroll: true});
      } else {
        const priorTabIndex = doc.body.getAttribute('tabindex');
        doc.body.setAttribute('tabindex', '-1');
        doc.body.focus({preventScroll: true});
        if (priorTabIndex === null) doc.body.removeAttribute('tabindex');
        else doc.body.setAttribute('tabindex', priorTabIndex);
      }
    }
    opener = null;
    // Release listeners attached by callers to the scrim/header as well as the
    // replaced body. The controller is reusable; an open creates fresh DOM.
    sheet.remove();
    sheet = panel = body = null;
  }

  function dispose(options) {
    close(options);
    sheet?.remove();
    sheet = panel = body = null;
    disposed = true;
  }

  return {open, close, dispose, get opened() { return opened; }, get disposed() { return disposed; }};
}

let controller;

export function openBottomSheet(title, html) {
  if (!controller || controller.disposed) controller = createBottomSheet();
  return controller.open(title, html);
}

export function closeBottomSheet() {
  controller?.close();
}

export function disposeBottomSheet() {
  controller?.dispose({restoreFocus: false});
  controller = null;
}
