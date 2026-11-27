// Compatibility model: bodies are copied unchanged from holdings.js.
// Financial fallback and grouping changes require a separate backend contract.
import { pnlPct } from '../../../utils.js';
import { typeLabel } from '../../../api.js';
export function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function cnyValue(r, key) {
  const cnyKey = `${key}_cny`;
  return r[cnyKey] ?? r[key] ?? 0;
}

export function safeNum(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

export function roundMoneyCents(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.sign(n || 1) * Math.round((Math.abs(n) + 1e-9) * 100) / 100;
}

export function subtractMoneyCents(left, right) {
  const leftNum = Number(left);
  const rightNum = Number(right);
  if (!Number.isFinite(leftNum) || !Number.isFinite(rightNum)) return null;
  return roundMoneyCents(leftNum - rightNum);
}

export function formatNumber(value, digits = 2) {
  return safeNum(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatCurrency(value) {
  return `¥${formatNumber(value, 2)}`;
}

export function formatCompactCurrency(value) {
  const n = safeNum(value);
  const sign = n < 0 ? '-' : '';
  return `${sign}¥${formatNumber(Math.abs(n), 2)}`;
}

export function formatSignedCurrency(value) {
  const n = safeNum(value);
  return `${n >= 0 ? '+' : '-'}¥${formatNumber(Math.abs(n), 2)}`;
}

export function signedMoneyClass(value) {
  return safeNum(value) >= 0 ? 'up' : 'down';
}

export function formatNativeDaily(value, currency) {
  const ccy = String(currency || 'CNY').trim().toUpperCase();
  if (!ccy || ccy === 'CNY') return '';
  const n = safeNum(value);
  const abs = Math.abs(n);
  const digits = ccy === 'KRW' ? 0 : 2;
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}${formatNumber(abs, digits)} ${ccy}`;
}

export function dailyFxText(row) {
  if (String(row?.currency || '').trim().toUpperCase() === 'CNY') return '';
  const value = row?.daily_fx_pnl_cny ?? row?.daily_fx_pnl;
  if (value == null || !Number.isFinite(Number(value))) return '证券汇兑 待核准';
  const fx = safeNum(value);
  if (Math.abs(fx) <= 0.005) return '';
  if (holdingDailyPnlPending(row)) return '证券汇兑 --';
  return `证券汇兑 ${formatSignedCurrency(fx)}`;
}

export function dailyNativeText(row) {
  const native = formatNativeDaily(row.daily_pnl_native ?? row.daily_pnl, row.currency);
  if (!native) return '';
  return holdingDailyPnlPending(row) ? `-- ${String(row.currency).trim().toUpperCase()}` : native;
}

export function holdingDailyPnlPending(row) {
  return row?.daily_pnl_stale === true
    || String(row?.daily_return_status || '').toLowerCase() === 'pending';
}

export function hasDailyTotalPnl(row) {
  return row && (row.daily_total_pnl_cny != null || row.daily_total_pnl != null);
}

export function datedReturnRow(row) {
  const recent = row?.recent_confirmed_return;
  if (row?.__isAggregated || !holdingDailyPnlPending(row) || recent?.status !== 'confirmed'
    || recent.fields?.daily_total_pnl_cny == null || !Number.isFinite(Number(recent.fields.daily_total_pnl_cny))) return row;
  return {
    ...row, ...recent.fields, daily_pnl_stale: false,
    daily_return_status: 'confirmed', daily_return_status_label: recent.status_label,
    daily_pnl_status: 'confirmed', daily_pnl_status_label: recent.status_label,
    __dailyReturnLabel: recent.label, __dailyReturnDate: recent.as_of,
    __dailyReturnNote: recent.current_label,
  };
}

export function displayDailyPnl(row) {
  return hasDailyTotalPnl(row) ? safeNum(cnyValue(row, 'daily_total_pnl')) : safeNum(cnyValue(row, 'daily_pnl'));
}

export function dailyPnlBreakdown(row) {
  const parts = [dailyNativeText(row), dailyFxText(row)].filter(Boolean);
  return parts.length ? parts.join(' · ') : '';
}

export function displayPnlPct(r) {
  if (!r.is_closed) return pnlPct(r);
  const cost = Number(cnyValue(r, 'cost') || 0);
  return cost > 0 ? (Number(cnyValue(r, 'holding_pnl') || 0) / cost * 100) : 0;
}

export function grossCnyValue(row, key) {
  const grossKey = `${key}_gross_cny`;
  if (Number.isFinite(Number(row[grossKey]))) return safeNum(row[grossKey]);
  const cnyKey = `${key}_cny`;
  if (Number.isFinite(Number(row[cnyKey]))) return safeNum(row[cnyKey]);
  return safeNum(row[key]);
}

export function cumulativePnl(row) {
  return safeNum(cnyValue(row, 'holding_pnl'));
}

export function currentFloatPnl(row) {
  return safeNum(cnyValue(row, 'market_value')) - safeNum(cnyValue(row, 'cost'));
}

export function holdingCostValue(row) {
  return safeNum(cnyValue(row, 'cost'));
}

export function realizedPnl(row) {
  if (Number.isFinite(Number(row.realized_trade_pnl_cny))) return safeNum(row.realized_trade_pnl_cny);
  return cumulativePnl(row) - currentFloatPnl(row);
}

export function hasRealizedPnl(value) {
  return Number.isFinite(value) && Math.abs(value) >= 0.005;
}

export function realizedPnlNote(value) {
  return hasRealizedPnl(value) ? `含已实现 ${formatSignedCurrency(value)}` : '';
}

export function dilutedAveragePrice(row) {
  const quantity = Number(row.quantity);
  if (!Number.isFinite(quantity) || Math.abs(quantity) <= 0) return null;
  if (row.diluted_cost_source === 'verified_trade_snapshot' && row.diluted_cost != null) {
    const value = Number(row.diluted_cost);
    return Number.isFinite(value) ? value / quantity : null;
  }
  const cost = grossCnyValue(row, 'cost');
  const realized = grossCnyValue(row, 'realized_trade_pnl');
  if (cost > 0 && Math.abs(realized) >= 0.005) return (cost - realized) / quantity;
  return holdingAveragePrice(row);
}

export function dilutedAveragePriceText(row) {
  const price = dilutedAveragePrice(row);
  if (!Number.isFinite(Number(price))) return '--';
  const text = formatNumber(price, holdingPriceDigits(row));
  const currency = String(row.currency || '').trim().toUpperCase();
  return currency && currency !== 'CNY' ? `${text} ${currency}` : text;
}

export function holdingGroupLabel(row) {
  const label = typeLabel(row.minor_category || row.type || row.major_category) || '其他';
  const normalized = {
    A股: 'A股个股',
    美股基金: '美股/海外',
    黄金: '贵金属',
    员工持股: '内部股份/分红',
  };
  return normalized[label] || label;
}

export function holdingGroupRank(label) {
  const order = ['A股个股', 'A股基金', '基金', '美股', '港股', '韩股', '美股/海外', '贵金属', '内部股份', 'ESOP', '现金/活期', '现金'];
  const index = order.findIndex((item) => label.includes(item));
  return index >= 0 ? index : order.length;
}

export function holdingGroupTone(label) {
  const text = String(label || '');
  if (text.includes('A股')) return 'cn';
  if (text.includes('美股') || text.includes('海外')) return 'global';
  if (text.includes('基金')) return 'fund';
  if (text.includes('贵金属') || text.includes('黄金')) return 'gold';
  if (text.includes('内部') || text.includes('ESOP')) return 'private';
  return 'other';
}

export function shortHoldingName(row) {
  const raw = String(row.name || row.symbol || '未命名资产');
  const symbol = String(row.symbol || '').trim();
  if (symbol && raw.startsWith(`${symbol} - `)) return raw.slice(symbol.length + 3);
  return raw;
}

export function holdingPriceDigits(row) {
  const label = holdingGroupLabel(row);
  if (label.includes('基金') || label.includes('贵金属')) return 4;
  return 2;
}

export function holdingPriceText(row) {
  if (!Number.isFinite(Number(row.current_price))) return '--';
  const price = formatNumber(row.current_price, holdingPriceDigits(row));
  const label = holdingGroupLabel(row);
  const currency = String(row.currency || '').trim().toUpperCase();
  if (currency && currency !== 'CNY' && (label.includes('美股') || String(row.major_category || '').includes('美股'))) {
    return `${price} ${currency}`;
  }
  return price;
}

export function holdingQuantityText(row) {
  const quantity = Number(row.quantity);
  if (!Number.isFinite(quantity) || Math.abs(quantity) <= 0) return '--';
  return formatNumber(quantity, 3).replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1');
}

export function holdingAveragePrice(row) {
  const direct = Number(row.buy_price);
  if (Number.isFinite(direct) && direct > 0) return direct;
  const quantity = Number(row.quantity);
  const cost = Number(row.cost);
  if (Number.isFinite(quantity) && quantity > 0 && Number.isFinite(cost) && cost > 0) {
    return cost / quantity;
  }
  return null;
}

export function holdingAveragePriceText(row) {
  const price = holdingAveragePrice(row);
  if (!Number.isFinite(Number(price))) return '--';
  const text = formatNumber(price, holdingPriceDigits(row));
  const currency = String(row.currency || '').trim().toUpperCase();
  return currency && currency !== 'CNY' ? `${text} ${currency}` : text;
}

export function holdingPriceChangePct(row) {
  return row.price_change_pct == null ? null : Number(row.price_change_pct) * 100;
}

export function signedPercentText(value) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const n = Number(value);
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

export function cashRow({ symbol, name, account, currency = 'CNY', nativeAmount = 0, amountCny = null, source = 'account' }) {
  const amount = safeNum(nativeAmount);
  const cnyAmount = Number.isFinite(Number(amountCny)) ? safeNum(amountCny) : amount;
  return {
    symbol,
    name,
    account,
    account_code: account,
    major_category: '现金',
    minor_category: '现金/活期',
    position_group: '现金/活期',
    type: '现金',
    currency,
    market_value: amount,
    market_value_cny: cnyAmount,
    position_net_value_cny: cnyAmount,
    market_value_native: amount,
    cost: amount,
    cost_cny: cnyAmount,
    holding_pnl: 0,
    holding_pnl_cny: 0,
    daily_pnl: 0,
    daily_pnl_cny: 0,
    current_price: null,
    quantity: null,
    data_source: source,
  };
}

export function buildAccountCashRows(accountValues, existingRows) {
  const snapshotAccounts = new Set(existingRows.filter(row => row.major_category === '现金')
    .map(row => row.account_code || row.account));
  const values = new Map((accountValues?.by_account || []).map(item => [item.account_code, item.portfolio_value]));
  const rows = [];
  const missing = [];
  for (const [account, label] of [['ibkr', 'IBKR'], ['tonghuashun', '同花顺']]) {
    if (snapshotAccounts.has(account)) continue;
    const evidence = values.get(account);
    if (account === 'ibkr' && Array.isArray(evidence?.cash_by_currency) && evidence.cash_by_currency.length) {
      for (const item of evidence.cash_by_currency) {
        const ccy = String(item.currency || evidence.base_currency || 'USD').toUpperCase();
        const cnyAmt = safeNum(item.amount_base);
        rows.push({
          ...cashRow({
            symbol: `IBKR_CASH_${ccy}`,
            name: `IBKR ${ccy} ${cnyAmt < 0 ? '负现金' : '现金'}`,
            account,
            currency: ccy,
            nativeAmount: item.native_amount,
            amountCny: item.amount_base,
            source: evidence.status,
          }),
          balance_as_of: item.as_of || evidence.cash_as_of,
        });
      }
      continue;
    }
    if (evidence?.cash_base == null || !Number.isFinite(Number(evidence.cash_base))) {
      missing.push(label);
      continue;
    }
    rows.push({
      ...cashRow({
        symbol: `${account.toUpperCase()}_CASH`, name: `${label}现金`, account,
        currency: account === 'ibkr' ? String(evidence.base_currency || 'USD').toUpperCase() : 'CNY',
        nativeAmount: evidence.cash_base, amountCny: evidence.cash_base, source: evidence.status,
      }),
      gross_cash_base: evidence.gross_cash_base ?? null,
      owner_ratio: evidence.owner_ratio ?? null,
      balance_as_of: evidence.cash_as_of,
    });
  }
  return {rows, missing};
}

export function historicalReferenceRow(reference) {
  return {
    account_code: reference.account_code,
    account_name: reference.account_name,
    symbol: reference.symbol,
    name: reference.name,
    currency: reference.currency,
    minor_category: reference.category,
    market_value_cny: reference.value,
    position_net_value_cny: reference.value,
    reference_as_of: reference.as_of,
    position_group: reference.position_group,
    position_segment: reference.position_segment,
  };
}

export function aggregateMobileHoldingRows(rows) {
  const groups = new Map();
  const display = [];
  for (const row of rows) {
    if (!row.combined_key) {
      display.push(row);
      continue;
    }
    if (!groups.has(row.combined_key)) groups.set(row.combined_key, []);
    groups.get(row.combined_key).push(row);
  }
  for (const members of groups.values()) {
    const first = members.find(row => row.combined_position);
    if (!first || members.length !== first.combined_count) display.push(...members);
    else display.push({...first, ...first.combined_position, account: 'mixed', account_code: 'mixed',
      account_name: '', __isAggregated: true, __accountRows: members});
  }
  return display.sort((a, b) => Number(b.market_value_cny ?? -Infinity) - Number(a.market_value_cny ?? -Infinity));
}

export function isInvestmentRow(row) {
  if (row.major_category !== '现金') return true;
  if (row.is_cash_management) return true;
  return ['ibkr', 'tonghuashun', 'tencent_licaitong_current_plus', 'wechat_lqt'].includes(row.account_code || row.account);
}
