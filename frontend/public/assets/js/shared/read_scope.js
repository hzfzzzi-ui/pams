export function isReadCancelled(error) {
  return error?.name === 'AbortError';
}

function cancelled(signal) {
  const error = new Error('Read cancelled');
  error.name = 'AbortError';
  if (signal?.reason !== undefined) error.cause = signal.reason;
  return error;
}

// One key represents one replaceable read, or a related parallel read group.
// The injected GET receives this scope's signal; POST never enters this helper.
export function createReadScope({get, signal, AbortController: Controller = globalThis.AbortController} = {}) {
  const pending = new Map();
  let disposed = false;
  function cancel(key) {
    const request = pending.get(key);
    if (!request) return;
    pending.delete(key);
    request.abort();
  }
  async function run(key, operation, callerSignal) {
    if (disposed || signal?.aborted) throw cancelled(signal);
    cancel(key);
    if (callerSignal?.aborted) throw cancelled(callerSignal);
    const request = new Controller();
    pending.set(key, request);
    const cancelCaller = () => request.abort(callerSignal.reason);
    callerSignal?.addEventListener('abort', cancelCaller, {once: true});
    try {
      const value = await operation(request.signal);
      // Injected readers may ignore a signal. Preserve rejection/late guards.
      if (request.signal.aborted) throw cancelled(request.signal);
      return value;
    } catch (error) {
      if (request.signal.aborted) throw cancelled(request.signal);
      // A failed parallel group may leave sibling GETs pending.
      request.abort();
      throw error;
    } finally {
      callerSignal?.removeEventListener('abort', cancelCaller);
      if (pending.get(key) === request) pending.delete(key);
    }
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    for (const key of pending.keys()) cancel(key);
    signal?.removeEventListener('abort', dispose);
  }
  if (signal?.aborted) dispose();
  else signal?.addEventListener('abort', dispose, {once: true});
  return {
    run, cancel, dispose,
    get: (key, path, options = {}) => run(key, ownedSignal => get(path, {...options, signal: ownedSignal}), options.signal),
    get disposed() {return disposed;},
  };
}

// Optional endpoint fallbacks remain for ordinary failures only. Cancellation
// must not turn an absent response into successful empty financial data.
export function readFallback(error, fallback) {
  if (isReadCancelled(error)) throw error;
  return fallback;
}
