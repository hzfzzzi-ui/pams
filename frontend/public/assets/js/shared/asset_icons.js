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
]);

export function assetIconMarkup(row, displayName = '') {
  const symbol = String(row?.symbol || row?.ticker || '').trim().toUpperCase();
  const mark = marks.get(symbol);
  const cleanName = String(displayName || row?.name || symbol || '资产').replace(/^[A-Z0-9.]+\s+-\s+/i, '').trim();
  const fallback = Array.from(cleanName || '资产').slice(0, 2).join('');
  const image = mark ? `<img src="${escapeHtml(new URL('../../images/asset-brands/' + mark[0], import.meta.url).href)}" alt="" loading="lazy">` : '';
  return `<span class="assetIdentityMark"${mark ? ` title="${escapeHtml(mark[1])}"` : ''} aria-hidden="true"><span>${escapeHtml(fallback)}</span>${image}</span>`;
}

export function assetIconCaption(row) {
  const symbol = String(row?.symbol || row?.ticker || '').trim().toUpperCase();
  const label = marks.get(symbol)?.[1] || '';
  return label.includes('基金管理机构') ? label : '';
}

export function installAssetIconFallback(root) {
  const onError = event => {
    if (event.target?.matches?.('.assetIdentityMark img')) event.target.remove();
  };
  root.addEventListener('error', onError, true);
  return () => root.removeEventListener('error', onError, true);
}
