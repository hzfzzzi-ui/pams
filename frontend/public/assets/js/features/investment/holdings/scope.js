export function createScopedDocument(root) {
  const doc = root.ownerDocument || root;
  return {
    body: root.body || root,
    getElementById: id => root.querySelector(`[id="${id}"]`),
    querySelector: selector => root.querySelector(selector),
    querySelectorAll: selector => root.querySelectorAll(selector),
    createElement: tag => doc.createElement(tag),
  };
}

export function createScope(host, read) {
  let active = true;
  const listeners = new Set(), timers = new Set(), observers = new Set(), charts = new Set();
  const abort = new AbortController();
  const cancelled = () => Object.assign(new Error('Investment page was disposed'), {name: 'AbortError'});
  function listen(target, type, callback, options) {
    if (!target?.addEventListener) return () => {};
    const guarded = event => { if (active) callback(event); };
    target.addEventListener(type, guarded, options);
    const remove = () => { target.removeEventListener(type, guarded, options); listeners.delete(remove); };
    listeners.add(remove);
    return remove;
  }
  return {
    get active() { return active; },
    signal: abort.signal,
    async get(path, options = {}) {
      if (!active) throw cancelled();
      const response = await read(path, {...options, signal: abort.signal});
      if (!active) throw cancelled();
      return response;
    },
    listen,
    timeout(callback, delay = 0) {
      if (!active) return null;
      const timer = host.setTimeout(() => { timers.delete(timer); if (active) callback(); }, delay);
      timers.add(timer); return timer;
    },
    observe(target, callback, options) {
      if (!active || typeof host.MutationObserver !== 'function') return null;
      const observer = new host.MutationObserver(() => { if (active) callback(); });
      observer.observe(target, options); observers.add(observer); return observer;
    },
    Chart(ChartClass) {
      return function OwnedChart(canvas, config) {
        if (!active) throw cancelled();
        const chart = new ChartClass(canvas, config);
        const destroy = chart.destroy.bind(chart);
        charts.add(chart);
        chart.destroy = () => { if (charts.delete(chart)) destroy(); };
        return chart;
      };
    },
    dispose() {
      if (!active) return;
      active = false; abort.abort();
      for (const remove of [...listeners]) remove();
      for (const timer of timers) host.clearTimeout(timer);
      for (const observer of observers) observer.disconnect();
      for (const chart of charts) {
        chart.destroy();
        const key = `__${chart.canvas?.id}`;
        if (host[key] === chart) delete host[key];
      }
      timers.clear(); observers.clear(); charts.clear();
    },
  };
}

export function createOwnedDetails(show, close) {
  let release = () => {};
  return {
    show(title, html) {
      release();
      const body = show(title, html), anchor = body?.firstElementChild;
      release = () => { if (anchor && body.isConnected && body.contains(anchor)) close?.(); release = () => {}; };
      return body;
    },
    dispose() { release(); },
  };
}

const errorOwners = new WeakMap();
export function createOwnedError(node) {
  const owner = {};
  return {
    show(error) {
      if (!node) return;
      errorOwners.set(node, owner);
      node.style.display = 'block'; node.textContent = '加载失败：' + String(error);
    },
    dispose() {
      if (node && errorOwners.get(node) === owner) {
        node.style.display = 'none'; node.textContent = ''; errorOwners.delete(node);
      }
    },
  };
}
