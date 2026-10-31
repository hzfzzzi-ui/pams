export function apiBase() {
  const host = window.location.hostname;
  const port = window.location.port;
  if (port === "" || port === "80" || port === "8080" || host === "pams.local") {
    return "";
  }
  return `http://${host}:8090`;
}

const API_CACHE_PREFIX = "pams.api.cache.v4:";
const apiMemoryCache = new Map();
const apiInFlight = new Map();

function pageRefreshToken() {
  try {
    return new URLSearchParams(window.location.search || "").get("refresh") || "";
  } catch {
    return "";
  }
}

function apiCacheTtl(path) {
  const raw = String(path || "");
  if (raw.includes("/api/analysis/lookthrough")) return 180_000;
  if (raw.includes("/api/analysis/return-attribution")) return 180_000;
  if (raw.includes("/investment/return-series")) return 90_000;
  if (raw.includes("/investment/section-return-series")) return 90_000;
  if (raw.includes("/holdings/by-category/")) return 45_000;
  if (raw.includes("/api/holdings/latest")) return 45_000;
  if (raw.includes("/api/accounts/overview")) return 45_000;
  if (raw.includes("/api/account-performance/")) return 45_000;
  if (raw.includes("/api/ibkr/performance")) return 45_000;
  if (raw.includes("/api/investment-returns/reported")) return 45_000;
  if (raw.includes("/api/ledger/workbench")) return 30_000;
  if (raw.includes("/api/sync/center")) return 45_000;
  if (raw.includes("/api/portfolio/valuation-state")) return 45_000;
  if (raw.includes("/api/portfolio/data-quality")) return 60_000;
  return 0;
}

function apiRequestTimeoutMs(path) {
  const raw = String(path || "");
  if (raw.includes("/api/portfolio/risk-disclosure")) return 120_000;
  if (raw.includes("/api/portfolio/config")) return 90_000;
  if (raw.includes("/api/analysis/lookthrough")) return 180_000;
  if (raw.includes("/api/analysis/return-attribution")) return 180_000;
  if (raw.includes("/nav/weekly")) return 90_000;
  if (raw.includes("/investment/return-series")) return 90_000;
  if (raw.includes("/investment/section-return-series")) return 90_000;
  return 30_000;
}

function normalizedApiCacheUrl(url) {
  try {
    const parsed = new URL(url, window.location.origin);
    const refresh = pageRefreshToken();
    if (refresh) parsed.searchParams.set("__page_refresh", refresh);
    parsed.searchParams.sort();
    const query = parsed.searchParams.toString();
    return `${parsed.origin}${parsed.pathname}${query ? `?${query}` : ""}`;
  } catch {
    return String(url || "");
  }
}

function apiCacheKey(url) {
  return `${API_CACHE_PREFIX}${encodeURIComponent(normalizedApiCacheUrl(url))}`;
}

function cacheStorage() {
  try {
    return window.sessionStorage || window.localStorage || null;
  } catch {
    return null;
  }
}

function readStoredCache(key, ttlMs) {
  const now = Date.now();
  const memory = apiMemoryCache.get(key);
  if (memory && now - memory.ts <= ttlMs) return memory.value;
  try {
    const storage = cacheStorage();
    const raw = storage?.getItem(key);
    if (!raw) return undefined;
    const cached = JSON.parse(raw);
    if (!cached || now - Number(cached.ts || 0) > ttlMs) {
      storage?.removeItem(key);
      apiMemoryCache.delete(key);
      return undefined;
    }
    apiMemoryCache.set(key, cached);
    return cached.value;
  } catch {
    return undefined;
  }
}

function writeStoredCache(key, value) {
  const entry = { ts: Date.now(), value };
  apiMemoryCache.set(key, entry);
  try {
    cacheStorage()?.setItem(key, JSON.stringify(entry));
  } catch {
    // Quota or private-mode failures should not block rendering.
  }
}

export function clearApiCache(match = "") {
  const needle = String(match || "");
  [...apiMemoryCache.keys()].forEach((key) => {
    if (!needle || key.includes(needle)) apiMemoryCache.delete(key);
  });
  [...apiInFlight.keys()].forEach((key) => {
    if (!needle || key.includes(needle)) apiInFlight.delete(key);
  });
  try {
    const storage = cacheStorage();
    if (!storage) return;
    for (let i = storage.length - 1; i >= 0; i -= 1) {
      const key = storage.key(i);
      if (key?.startsWith(API_CACHE_PREFIX) && (!needle || key.includes(needle))) {
        storage.removeItem(key);
      }
    }
  } catch {
    // Ignore storage access errors.
  }
}

export function warmApiCache(paths, options = {}) {
  const list = [...new Set((Array.isArray(paths) ? paths : [paths]).filter(Boolean))];
  if (!list.length) return Promise.resolve([]);
  const run = () => Promise.allSettled(list.map((path) => jget(path, { ...options, prefetch: true })));
  if (options.delayMs) {
    return new Promise((resolve) => {
      setTimeout(() => run().then(resolve), options.delayMs);
    });
  }
  return run();
}

function headerEntries(headers = {}) {
  if (!headers) return [];
  if (typeof headers.forEach === "function") {
    const entries = [];
    headers.forEach((value, key) => entries.push([key, value]));
    return entries;
  }
  return Object.entries(headers);
}

function xhrWithTimeout(url, init = {}, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(init.method || "GET", url, true);
    xhr.timeout = timeoutMs;
    headerEntries(init.headers).forEach(([key, value]) => {
      if (value != null) xhr.setRequestHeader(key, value);
    });
    xhr.onload = () => {
      const body = xhr.responseText || "";
      resolve({
        ok: xhr.status >= 200 && xhr.status < 300,
        status: xhr.status,
        statusText: xhr.statusText || "",
        text: async () => body,
        json: async () => (body ? JSON.parse(body) : null),
      });
    };
    xhr.onerror = () => reject(new Error("Network request failed"));
    xhr.onabort = () => reject(new Error("Request aborted"));
    xhr.ontimeout = () => {
      const error = new Error(`Request timeout (${Math.round(timeoutMs / 1000)}s)`);
      error.name = "AbortError";
      reject(error);
    };
    xhr.send(init.body ?? null);
  });
}

async function fetchWithTimeout(url, init = {}, timeoutMs = 30000) {
  if (typeof fetch === "function") {
    if (typeof AbortController !== "function") {
      return fetch(url, init);
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      return await fetch(url, { ...init, signal: ctrl.signal });
    } finally {
      clearTimeout(timer);
    }
  }
  if (typeof XMLHttpRequest === "function") {
    return xhrWithTimeout(url, init, timeoutMs);
  }
  throw new Error("No browser HTTP transport available");
}

export const TYPE_LABELS = {
  cash: "现金",
  "现金": "现金",
  fund: "基金",
  "基金": "基金",
  stock: "A股",
  stock_cn: "A股",
  "A股": "A股",
  stock_us: "美股",
  "美股": "美股",
  wealth: "理财",
  "理财": "理财",
  gold: "黄金",
  "黄金": "黄金",
  asset_gold: "黄金",
  crypto: "加密",
  "加密": "加密",
  equity_cn: "A股权益",
  equity_us: "美股权益",
  esop: "员工持股",
  equity_emp: "员工持股",
  asset_hpf: "公积金",
  hpf: "公积金",
  medical_insurance: "医保",
  asset_medical_insurance: "医保",
  receivable: "应收款",
  recv: "应收款",
  liability: "负债",
  "负债": "负债",
  loan: "借款",
  "借款": "借款",
};

export const ACCOUNT_LABELS = {
  tencent_licaitong: "腾讯理财通",
  licaitong: "腾讯理财通",
  main: "腾讯理财通",
  alipay: "支付宝",
  zhifubao: "支付宝",
  ccb: "建行",
  ccb_cny: "建行",
  ccb_wm47: "建行",
  ccb_gold: "建行",
  ccb_7332: "建行",
  cgb_cny: "广发",
  huawei: "华为账户",
  huawei_employer: "华为雇主福利",
  huawei_stock: "华为 ESOP",
  huawei_emp: "华为 ESOP",
  huawei_esop: "华为 ESOP",
  cn_hpf: "公积金账户",
  hpf: "公积金账户",
  medical_insurance: "医保账户",
  binance: "币安",
  ibkr: "盈透证券",
  IBKR: "盈透证券",
  us_main: "盈透证券",
  gf: "广发",
  guangfa: "广发",
  gf_7443: "广发",
  gf_9711: "广发",
  icbc_6689: "工行",
  icbc_6689_cny: "工行",
  icbc_6689_usd: "工行",
  santander: "桑坦德银行",
  santander4209: "桑坦德银行",
  santander4209_cash: "桑坦德银行",
  tonghuashun: "同花顺",
  ths: "同花顺",
  cn_a: "同花顺",
  cn_a_rmb: "同花顺",
  recv_loan: "借款",
  loan: "借款",
  wechat: "微信",
  weixin: "微信",
  recv_reimburse: "报销应收款",
  manual: "手动录入",
  unknown: "未知",
};

export function accountLabel(a) {
  return ACCOUNT_LABELS[a] || a || "";
}

export function typeLabel(t) {
  return TYPE_LABELS[t] || t || "";
}

export async function jget(path, options = {}) {
  const url = apiBase() + path;
  const ttlMs = options.ttlMs ?? apiCacheTtl(path);
  const cacheKey = ttlMs > 0 ? apiCacheKey(url) : "";
  if (ttlMs > 0 && options.cache !== false && !options.force) {
    const cached = readStoredCache(cacheKey, ttlMs);
    if (cached !== undefined) return cached;
    const inFlight = apiInFlight.get(cacheKey);
    if (inFlight) return inFlight;
  }
  const requestPromise = (async () => {
    let res;
    const timeoutMs = apiRequestTimeoutMs(path);
    try {
      res = await fetchWithTimeout(url, { cache: "no-store" }, timeoutMs);
    } catch (e) {
      if (e && e.name === "AbortError") throw new Error(`Request timeout (${Math.round(timeoutMs / 1000)}s)`);
      throw e;
    }
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = await res.json();
    if (ttlMs > 0 && options.cache !== false) writeStoredCache(cacheKey, data);
    return data;
  })();
  if (ttlMs > 0 && options.cache !== false && !options.force) {
    apiInFlight.set(cacheKey, requestPromise);
    requestPromise.then(
      () => apiInFlight.delete(cacheKey),
      () => apiInFlight.delete(cacheKey),
    );
  }
  return requestPromise;
}

export async function jpost(path, bodyObj) {
  let res;
  try {
    res = await fetchWithTimeout(
      apiBase() + path,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: bodyObj ? JSON.stringify(bodyObj) : undefined,
      },
      90000,
    );
  } catch (e) {
    if (e && e.name === "AbortError") throw new Error("Request timeout (90s)");
    throw e;
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const data = await res.json();
  clearApiCache();
  return data;
}

export function fmt2(x) {
  if (x == null || x === "") return "";
  const n = Number(x);
  if (Number.isNaN(n)) return String(x);
  return n.toLocaleString("zh-CN", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
}

export function currencySymbol(currency = "CNY") {
  const code = String(currency || "CNY").toUpperCase();
  return {
    CNY: "¥",
    USD: "$",
    HKD: "HK$",
    JPY: "¥",
    MXN: "MX$",
  }[code] || `${code} `;
}

export function fmtCompactNumber(value, digits = 0) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_000_000) {
    return `${sign}${(abs / 1_000_000).toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}M`;
  }
  if (abs >= 10_000) {
    return `${sign}${(abs / 1_000).toLocaleString("zh-CN", {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    })}K`;
  }
  return `${sign}${abs.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function fmtMoneyCompact(value, currency = "CNY", digits = 0) {
  return fmtMoneyFixed2(value, currency);
}

export function fmtSignedMoneyCompact(value, currency = "CNY", digits = 0) {
  return fmtSignedMoneyFixed2(value, currency);
}

export function fmtMoneyFixed2(value, currency = "CNY") {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return `${n < 0 ? "-" : ""}${currencySymbol(currency)}${Math.abs(n).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function fmtSignedMoneyFixed2(value, currency = "CNY") {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return `${n >= 0 ? "+" : "-"}${currencySymbol(currency)}${Math.abs(n).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function fmtPercentCompact(value, digits = 1) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return `${(n * 100).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}%`;
}
