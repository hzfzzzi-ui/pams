import { escapeHtml } from '../utils.js';

// Artwork is the corresponding issuer's official-site icon. Fund artwork is
// explicitly identified as the manager's mark rather than a fund logo.
const marks = new Map([
  ['HUAWEI_ESOP', ['huawei.png', '华为']],
  ['005930.KS', ['samsung.png', '三星电子']],
  ['NVDA', ['nvidia.ico', 'NVIDIA']],
  ['MU', ['micron.png', '美光科技']],
  ['GOOG', ['google.ico', 'Alphabet']],
  ['GOOGL', ['google.ico', 'Alphabet']],
  ['3690', ['meituan.ico', '美团']],
  ['3690.HK', ['meituan.ico', '美团']],
  ['PLTR', ['palantir.ico', 'Palantir']],
  ['TSLA', ['tesla.jpg', 'Tesla']],
  ['SPCX', ['spacex.ico', 'SpaceX']],
  ['CBRS', ['cerebras.png', 'Cerebras']],
  ['QQQ', ['invesco.ico', '景顺（基金管理机构）']],
  ['603986', ['gigadevice.png', '兆易创新']],
  ['603986.SH', ['gigadevice.png', '兆易创新']],
  ['601899', ['zijin.ico', '紫金矿业']],
  ['601899.SH', ['zijin.ico', '紫金矿业']],
  ['002281', ['accelink.ico', '光迅科技']],
  ['002281.SZ', ['accelink.ico', '光迅科技']],
  ['110020', ['efunds.ico', '易方达（基金管理机构）']],
  ['016186', ['gffunds.png', '广发基金（基金管理机构）']],
  ['025197', ['gffunds.png', '广发基金（基金管理机构）']],
  ['270042', ['gffunds.png', '广发基金（基金管理机构）']],
  ['020640', ['gffunds.png', '广发基金（基金管理机构）']],
  ['501016', ['guotai.png', '国泰基金（基金管理机构）']],
  ['016440', ['huaxia.ico', '华夏基金（基金管理机构）']],
  ['024239', ['huaxia.ico', '华夏基金（基金管理机构）']],
  ['008585', ['huaxia.ico', '华夏基金（基金管理机构）']],
  ['011613', ['huaxia.ico', '华夏基金（基金管理机构）']],
  ['001412', ['debang.jpg', '德邦基金（基金管理机构）']],
  ['022664', ['htbr.jpg', '华泰柏瑞基金（基金管理机构）']],
  ['012752', ['ccb_fund.jpg', '建信基金（基金管理机构）']],
  ['539001', ['ccb_fund.jpg', '建信基金（基金管理机构）']],
  ['009033', ['ccb_fund.jpg', '建信基金（基金管理机构）']],
  ['000216', ['huaan.jpg', '华安基金（基金管理机构）']],
  ['004419', ['huitianfu.ico', '汇添富基金（基金管理机构）']],
  ['020839', ['nanfang.jpg', '南方基金（基金管理机构）']],
  ['161226', ['sdic.jpg', '国投瑞银基金（基金管理机构）']],
]);

function findMark(row, displayName = '') {
  const symbol = String(row?.symbol || row?.ticker || '').trim().toUpperCase();
  const name = String(displayName || row?.name || '').toUpperCase();
  // These are local identifiers for private holdings, not exchange tickers.
  if (symbol === 'SPCX' && !/SPACE ?X|SPACE EXPLORATION/.test(name)) return null;
  if (symbol === 'CBRS' && !/CEREBRAS/.test(name)) return null;
  const mark = marks.get(symbol);
  if (mark?.[1].includes('基金管理机构') && symbol !== 'QQQ') {
    const manager = mark[1].replace(/基金（基金管理机构）|（基金管理机构）/g, '').toUpperCase();
    if (!name.includes(manager)) return null;
  }
  return mark;
}

export function assetIconMarkup(row, displayName = '') {
  const symbol = String(row?.symbol || row?.ticker || '').trim().toUpperCase();
  const mark = findMark(row, displayName);
  const cleanName = String(displayName || row?.name || symbol || '资产').replace(/^[A-Z0-9.]+\s+-\s+/i, '').trim();
  const fallback = Array.from(cleanName || '资产').slice(0, 2).join('');
  const image = mark ? `<img src="${escapeHtml(new URL('../../images/asset-brands/' + mark[0], import.meta.url).href)}" alt="" loading="lazy">` : '';
  return `<span class="assetIdentityMark"${mark ? ` title="${escapeHtml(mark[1])}"` : ''} aria-hidden="true"><span>${escapeHtml(fallback)}</span>${image}</span>`;
}

export function assetIconCaption(row) {
  const label = findMark(row)?.[1] || '';
  return label.includes('基金管理机构') ? label : '';
}

export function installAssetIconFallback(root) {
  const onError = event => {
    if (event.target?.matches?.('.assetIdentityMark img')) event.target.remove();
  };
  root.addEventListener('error', onError, true);
  return () => root.removeEventListener('error', onError, true);
}
