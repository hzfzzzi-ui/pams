// Import keeps its private no-store client. Reads are owned by the mounted page;
// accepted writes are never aborted, retried, or replayed during a remount.
export function createRequestScope(client, { recovery = null, getBatch = () => null } = {}) {
  let alive = true;
  // This handle survives BFCache remounts in memory. A disposed request may
  // update its outcome only; it cannot render, retry, or continue a write chain.
  recovery ||= { pending: 0, requiresReview: false, last: null };
  const reads = new Set();
  const cancelled = () => Object.assign(new Error('导入页面已退出'), {name: 'AbortError'});
  const check = () => { if (!alive) throw cancelled(); };
  async function request(path, method = 'GET', body, raw = false, signal) {
    check();
    const read = method === 'GET';
    if (!read && recovery.requiresReview) {
      throw Object.assign(new Error('上次操作结果尚未确认，请先载入服务器版本。'), {code: 'write_result_unconfirmed'});
    }
    const controller = read ? new AbortController() : null;
    let operation;
    const abort = () => controller?.abort(signal?.reason);
    if (read) {
      if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, {once: true});
      reads.add(controller);
    } else {
      const batch = getBatch();
      operation = { path, method, batchId: batch?.batch_id ?? null,
        revision: body?.revision ?? batch?.revision ?? null, outcome: 'pending', result: null, error: null };
      recovery.last = operation;
      recovery.pending++;
    }
    try {
      if (controller?.signal.aborted) throw cancelled();
      const value = await client(path, method, body, raw, controller?.signal);
      if (operation) {
        const result = value?.batch || value;
        operation.outcome = 'succeeded';
        operation.result = {batchId: result?.batch_id ?? null, revision: result?.revision ?? null, status: result?.status ?? null};
      }
      check();
      if (controller?.signal.aborted) throw cancelled();
      return value;
    } catch (error) {
      if (operation?.outcome === 'pending') {
        // Explicit client rejections are definitive. Network errors, timeouts
        // and server failures cannot establish whether the write committed.
        const rejected = Number.isInteger(error.status) && error.status >= 400 && error.status < 500 && ![408, 499].includes(error.status);
        operation.outcome = rejected ? 'rejected' : 'unknown';
        operation.error = {status: error.status ?? null, code: error.code ?? null};
        if (!rejected) recovery.requiresReview = true;
      }
      throw error;
    } finally {
      if (read) { reads.delete(controller); signal?.removeEventListener('abort', abort); }
      else recovery.pending--;
    }
  }
  return {
    request,
    recovery,
    isActive: () => alive,
    hasPendingWrite: () => recovery.pending > 0,
    acknowledgeRecovery() {
      if (recovery.pending > 0) return false;
      recovery.requiresReview = false;
      recovery.last = null;
      return true;
    },
    dispose() {
      if (!alive) return;
      alive = false;
      if (recovery.pending > 0) recovery.requiresReview = true;
      for (const controller of reads) controller.abort();
      reads.clear();
    },
  };
}
