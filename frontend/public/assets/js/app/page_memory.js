const key = Symbol.for('pams.pageMemory');

export function installPageMemory(host, frame) {
  const entries = new Map();
  const read = (slot, variant) => {
    const entry = entries.get(slot);
    if (entry?.variant === variant) return structuredClone(entry.value);
  };
  host[key] = {
    forDocument(doc) {
      const page = doc.location.pathname.split('/').pop();
      if (!['investments.html', 'ledger.html'].includes(page) || frame.contentDocument !== doc) return null;
      const slotKey = slot => page + ':' + slot;
      return {
        read: (slot, variant) => read(slotKey(slot), variant),
        write(slot, variant, value) {
          if (frame.contentDocument === doc) entries.set(slotKey(slot), {variant, value: structuredClone(value)});
        },
      };
    },
    clearData() {
      for (const slot of entries.keys()) {
        if (!slot.endsWith(':view')) entries.delete(slot);
      }
    },
  };
  return {read};
}

export function pageMemory(host) {
  try {
    if (host?.parent !== host) return host.parent[key]?.forDocument(host.document);
  } catch { /* A standalone or differently embedded page has no shell cache. */ }
}

export function pageDataInvalidator(host = globalThis.window) {
  try {
    const memory = host?.parent[key];
    return () => memory?.clearData();
  } catch { return () => {}; }
}

export function pageScroller(host) {
  const doc = host.document;
  // The mobile content document scrolls its body. Wider layouts can scroll the root.
  if (doc.body && /^(auto|scroll)$/.test(host.getComputedStyle(doc.body).overflowY)) return doc.body;
  return doc.scrollingElement;
}

export function restorePageScroll(host, top, isCurrent) {
  if (!top) return;
  const hash = host.location.hash;
  const restore = () => host.requestAnimationFrame(() => {
    if (!isCurrent() || host.location.hash !== hash) return;
    const scroller = pageScroller(host);
    if (!scroller) return;
    // Keep the saved offset while totals above the restored list update.
    scroller.style.overflowAnchor = 'none';
    scroller.scrollTo({top, behavior:'instant'});
  });
  // The shell applies the final scrolling geometry when the frame finishes loading.
  if (host.document.documentElement.classList.contains('pams-content')) restore();
  else host.addEventListener('pams:frame-ready', restore, {once:true});
}

export function showRememberedData(element, state = '') {
  if (!element) return;
  element.textContent = state === 'failed' ? '更新失败，显示上次数据' : state ? '显示上次数据，正在更新…' : '';
  element.hidden = !state;
}
