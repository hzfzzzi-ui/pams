export function apiBase(){
  // 自动检测是否通过Caddy网关还是直接访问
  const host = window.location.hostname;
  const port = window.location.port;

  // 如果是通过默认端口(80)或pams.local访问，使用相对路径(通过Caddy)
  if (port === '' || port === '80' || host === 'pams.local') {
    return '';  // Caddy会路由所有API请求
  }

  // 否则直接访问API端口8090 (注意：大部分端点没有/api前缀)
  return `http://${host}:8090`;
}

export const TYPE_LABELS = {
  'cash': '现金',
  'fund': '基金',
  'stock_cn': 'A股',
  'stock_us': '美股',
  'wealth': '理财',
  'gold': '黄金',
  'crypto': '加密',
};

export const VENUE_LABELS = {
  'guosen': '国信证券',
  'tencent_licaitong': '腾讯理财通',
  'ibkr': '盈透证券',
};

export function venueLabel(v){
  return VENUE_LABELS[v] || v || '';
}

export const ACCOUNT_LABELS = {
  'cn_a_rmb': 'A股人民币账户',
  'main': '主账户',
  'us_main': '美股主账户',
};

export function accountLabel(a){
  return ACCOUNT_LABELS[a] || a || '';
}

export function typeLabel(t){
  return TYPE_LABELS[t] || t || '';
}

export async function jget(path){
  const res = await fetch(apiBase()+path, { cache: 'no-store' });
  if(!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export async function jpost(path, bodyObj){
  const res = await fetch(apiBase()+path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: bodyObj ? JSON.stringify(bodyObj) : undefined,
  });
  if(!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export function fmt2(x){
  if(x==null || x==='') return '';
  const n = Number(x);
  if (Number.isNaN(n)) return String(x);
  return n.toLocaleString('zh-CN',{maximumFractionDigits:2});
}
