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
  // 资产类型映射
  'cash': '现金',
  '现金': '现金',

  'fund': '基金',
  '基金': '基金',

  'stock': 'A股',
  'stock_cn': 'A股',
  'A股': 'A股',

  'stock_us': '美股',
  '美股': '美股',

  'wealth': '理财',
  '理财': '理财',

  'gold': '黄金',
  '黄金': '黄金',
  'asset_gold': '黄金',

  'crypto': '加密',
  '加密': '加密',

  'equity_cn': 'A股权益',
  'equity_us': '美股权益',

  'esop': '员工持股',
  'equity_emp': '员工持股',

  'asset_hpf': '公积金',
  'hpf': '公积金',

  'receivable': '应收款',
  'recv': '应收款',

  'liability': '负债',
  '负债': '负债',

  'loan': '借款',
  '借款': '借款',
};

export const ACCOUNT_LABELS = {
  // 标准账户映射（基于资产负债表）
  'tencent_licaitong': '腾讯理财通',
  'licaitong': '腾讯理财通',
  'main': '腾讯理财通',  // 主账户默认映射到腾讯理财通

  'ccb': '建行',
  'ccb_wm47': '建行',
  'ccb_gold': '建行',
  'ccb_7332': '建行',

  'huawei': '华为账户',
  'huawei_emp': '华为账户',
  'huawei_esop': '华为账户',

  'cn_hpf': '公积金账户',
  'hpf': '公积金账户',

  'binance': '币安',

  'ibkr': '盈透证券',
  'us_main': '盈透证券',  // 美股主账户映射到盈透证券

  'gf': '广发',
  'guangfa': '广发',
  'gf_7443': '广发',
  'gf_9711': '广发',

  'santander': '桑坦德银行',
  'santander4209': '桑坦德银行',
  'santander4209_cash': '桑坦德银行',

  'tonghuashun': '同花顺',
  'ths': '同花顺',
  'cn_a': '同花顺',
  'cn_a_rmb': '同花顺',  // A股人民币账户映射到同花顺

  'recv_loan': '借款',
  'loan': '借款',

  'wechat': '微信',
  'weixin': '微信',

  // 其他特殊账户
  'recv_reimburse': '报销应收款',
  'manual': '手动录入',
  'unknown': '未知',
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
  return n.toLocaleString('zh-CN',{minimumFractionDigits:3,maximumFractionDigits:3});
}
