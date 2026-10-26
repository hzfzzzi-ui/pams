const pending = new Map();
const waiting = new Set();
const MAX_WAIT_MS = 10000;

function track(key, promise) {
  pending.set(key, promise);
  const settled = () => {
    if (pending.get(key) === promise) pending.delete(key);
    for (const check of [...waiting]) check();
  };
  Promise.resolve(promise).then(settled, settled);
  return promise;
}

export function trackInvestmentNav(promise) {
  return track('nav', promise);
}

export function trackInvestmentHoldings(promise) {
  return track('holdings', promise);
}

function afterReady(task, ready, tab) {
  if (!document.querySelector('[data-invest-tab]') || ready()) {
    return Promise.resolve().then(task);
  }
  return new Promise((resolve, reject) => {
    let started = false;
    let timer;
    let observer;
    const start = () => {
      if (started) return;
      started = true;
      clearTimeout(timer);
      observer?.disconnect();
      waiting.delete(check);
      Promise.resolve().then(task).then(resolve, reject);
    };
    const check = () => {
      if (ready() || (tab && document.body.dataset.investTab === tab)) start();
    };
    waiting.add(check);
    if (tab && typeof MutationObserver === 'function') {
      observer = new MutationObserver(check);
      observer.observe(document.body, { attributes: true, attributeFilter: ['data-invest-tab'] });
    }
    // A slow primary request must not indefinitely starve the other panels.
    timer = setTimeout(start, MAX_WAIT_MS);
    check();
  });
}

export function afterInvestmentNav(task, tab) {
  return afterReady(task, () => !pending.has('nav'), tab);
}

export function afterInvestmentStartup(task, tab) {
  return afterReady(task, () => !pending.size, tab);
}
