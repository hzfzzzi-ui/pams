import { ROOT } from './contract.js?v=20261031-modules-v1';

export function createImportClient(fetcher) {
  return async (path, method = 'GET', body, raw = false, signal) => {
    if (!path.startsWith('/') || path.startsWith('//') || /[.#]/.test(path.split('?')[0])) throw new Error('无效导入路径');
    const response = await fetcher(ROOT + path, { method, signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: body === undefined ? {} : { 'Content-Type': raw ? (body.type || 'application/octet-stream') : 'application/json' },
      body: body === undefined ? undefined : raw ? body : JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || `请求失败（${response.status}）`), { status: response.status, code: data.detail?.code });
    return data;
  };
}
