// DOM ownership and cleanup for this feature only. The shared sheet may also be
// used by other investment modules; never read or close their replacement panel.
export function createScope(root, {win = root.ownerDocument?.defaultView, openSheet, closeSheet}) {
  let alive = true;
  let portal = null;
  let anchor = null;
  let version = 0;
  const listeners = [];
  const windowListeners = [];
  const timers = new Set();
  const frames = new Set();
  const ownsPortal = () => Boolean(portal?.isConnected && anchor && portal.contains(anchor));
  const validNode = node => {
    if (!node) return false;
    const sheet = node.closest?.('.appBottomSheet');
    if (sheet) return ownsPortal() && portal.contains(node);
    return root.contains(node);
  };
  function query(selector) {
    const inPortal = ownsPortal() ? portal.querySelector(selector) : null;
    if (inPortal) return inPortal;
    const node = root.querySelector(selector);
    return validNode(node) ? node : null;
  }
  function byId(id) {
    const escaped = String(id).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    return query(`[id="${escaped}"]`);
  }
  function releasePortal() {
    if (portal) for (const {type, listener, options} of listeners) portal.removeEventListener(type, listener, options);
    if (ownsPortal()) closeSheet();
    portal = anchor = null;
  }
  function openOwnedSheet(title, html) {
    if (!alive) return null;
    releasePortal();
    version++;
    portal = openSheet(title, html);
    anchor = portal?.firstElementChild;
    if (portal && !root.contains(portal)) {
      for (const {type, listener, options} of listeners) portal.addEventListener(type, listener, options);
    }
    return portal;
  }
  function closeOwnedSheet() {version++; releasePortal();}
  function on(type, callback, options) {
    const listener = event => {if (alive && validNode(event.target)) callback(event);};
    listeners.push({type, listener, options});
    root.addEventListener(type, listener, options);
    if (ownsPortal() && !root.contains(portal)) portal.addEventListener(type, listener, options);
  }
  function onWindow(type, callback, options) {
    const listener = event => {if (alive) callback(event);};
    windowListeners.push({type, listener, options}); win?.addEventListener(type, listener, options);
  }
  function delay(callback, ms) {
    const id = win.setTimeout(() => {timers.delete(id); if (alive) callback();}, ms);
    timers.add(id); return id;
  }
  function frame(callback) {
    const opened = version;
    const id = win.requestAnimationFrame(() => {frames.delete(id); if (alive && opened === version) callback();});
    frames.add(id); return id;
  }
  function isCurrentForm(form) {
    return alive && ownsPortal() && form?.isConnected && portal.querySelector('#tradeReviewForm') === form;
  }
  function dispose() {
    if (!alive) return;
    alive = false; version++;
    for (const {type, listener, options} of listeners) root.removeEventListener(type, listener, options);
    for (const {type, listener, options} of windowListeners) win?.removeEventListener(type, listener, options);
    for (const id of timers) win.clearTimeout(id);
    for (const id of frames) win.cancelAnimationFrame(id);
    releasePortal(); listeners.length = 0; windowListeners.length = 0; timers.clear(); frames.clear();
  }
  return {root, win, byId, query, on, onWindow, delay, frame, openSheet: openOwnedSheet, closeSheet: closeOwnedSheet,
    isCurrentForm, dispose, get alive() {return alive;}, get sheetVersion() {return version;}};
}
