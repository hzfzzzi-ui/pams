const institutions = new Map([
  ['ccb', ['建行', 'ccb']],
  ['icbc_6689', ['工行', 'icbc']],
  ['santander', ['Santander', 'santander']],
  ['tencent', ['腾讯', 'wechat']],
  ['alipay', ['支付宝', 'alipay']],
  ['ibkr', ['IBKR', 'ibkr']],
  ['tonghuashun', ['同花顺', 'tonghuashun']],
  ['benefits', ['五险一金']],
  ['guangfa_credit', ['广发', 'guangfa']],
]);

const aliases = new Map([
  ['wechat_root', 'tencent'],
  ['guangfa_root', 'guangfa_credit'],
  ['santander_root', 'santander'],
  ['alipay_root', 'alipay'],
  ['ibkr_root', 'ibkr'],
  ['tonghuashun_root', 'tonghuashun'],
  ['benefits_root', 'benefits'],
]);

export function accountInstitution(code, fallbackLabel) {
  const [label, artwork] = institutions.get(aliases.get(code) || code) || [fallbackLabel];
  return { label, logoUrl: artwork ? new URL('../../images/account-brands/' + artwork + '.jpg', import.meta.url).href : null };
}
