// Own one panel's pending startup/read and event lifetime. Network cancellation
// depends on the injected reader; generations also suppress late results.
export function createReadSession({win, afterStartup, tab, read, apply, onError}) {
  const events = new win.AbortController();
  let disposed = false;
  let generation = 0;
  let pending;

  async function reload() {
    if (disposed) return;
    const current = ++generation;
    pending?.abort();
    pending = null;
    try {
      return await afterStartup(async () => {
        if (disposed || current !== generation) return;
        const request = new win.AbortController();
        pending = request;
        try {
          const payload = await read(request.signal);
          if (!disposed && current === generation) apply(payload);
        } finally {
          if (pending === request) pending = null;
        }
      }, tab);
    } catch (error) {
      if (!disposed && current === generation) onError(error);
    }
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    generation++;
    pending?.abort();
    pending = null;
    events.abort();
  }

  return {reload, dispose, signal: events.signal, get disposed() { return disposed; }};
}
