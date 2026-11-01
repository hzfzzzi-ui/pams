// Compatibility model: bodies are copied unchanged from holdings.js.
// Financial fallback and grouping changes require a separate backend contract.
import { pnlPct } from '../../../utils.js?v=20261031-modules-v1';
import { makeAccountCashHoldingRows } from '../../../account_cash.js?v=20261031-modules-v1';
import { typeLabel } from '../../../api.js?v=20261031-modules-v1';
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

export function mergedDailyPnlStatus(rows) {
  if (rows.some(holdingDailyPnlPending)) return { status: 'pending', label: '' };
  const statuses = rows.map((row) => String(row?.daily_return_status || row?.daily_pnl_status || '').toLowerCase()).filter(Boolean);
  if (statuses.includes('estimated')) return { status: 'estimated', label: '预估收益' };
  if (statuses.includes('confirmed')) return { status: 'confirmed', label: '今日收益已确认' };
  return { status: '', label: '' };
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

export function hasRealizedPnl(row) {
  return Math.abs(realizedPnl(row)) >= 0.005;
}

export function realizedPnlNote(row) {
  if (!hasRealizedPnl(row)) return '—';
  return `含已实现 ${formatSignedCurrency(realizedPnl(row))}`;
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
  const order = ['A股个股', 'A股基金', '基金', '美股', '美股/海外', '贵金属', '内部股份', 'ESOP', '现金/活期', '现金'];
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
  const direct = Number(row.price_change_pct);
  if (Number.isFinite(direct)) return direct * 100;
  const daily = safeNum(cnyValue(row, 'daily_pnl'));
  const value = safeNum(cnyValue(row, 'market_value'));
  const base = value - daily;
  if (Math.abs(daily) > 0.005 && Math.abs(base) > 0.005) return daily / base * 100;
  return null;
}

export function signedPercentText(value) {
  if (!Number.isFinite(Number(value))) return '--';
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
    type: '现金',
    currency,
    market_value: amount,
    market_value_cny: cnyAmount,
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

export function buildAccountCashRows(accountOverview, fxRates = {}, ibkrPerf = null) {
  const rows = makeAccountCashHoldingRows(accountOverview, fxRates);

  const accounts = Array.isArray(accountOverview?.accounts) ? accountOverview.accounts : [];
  const tonghuashun = accounts.find((account) => account?.account_code === 'tonghuashun');
  const tonghuashunTotal = Number(tonghuashun?.primary_balance?.amount);
  const tonghuashunPositions = Number(tonghuashun?.platform_balance?.amount);
  if (Number.isFinite(tonghuashunTotal) && Number.isFinite(tonghuashunPositions)) {
    const tonghuashunCash = subtractMoneyCents(tonghuashunTotal, tonghuashunPositions);
    if (Math.abs(tonghuashunCash) >= 0.005) {
      rows.push(cashRow({
        symbol: 'TONGHUASHUN_CASH',
        name: '\u540c\u82b1\u987a\u73b0\u91d1',
        account: 'tonghuashun',
        currency: 'CNY',
        nativeAmount: tonghuashunCash,
        amountCny: tonghuashunCash,
        source: tonghuashun?.primary_balance?.source || 'account_overview',
      }));
    }
  }

  const ibkrCashCny = Number.isFinite(Number(ibkrPerf?.cash_cny)) ? safeNum(ibkrPerf.cash_cny) : null;
  if (ibkrCashCny != null && Math.abs(ibkrCashCny) >= 0.005) {
    rows.unshift(cashRow({
      symbol: 'IBKR_CASH',
      name: 'IBKR现金',
      account: 'ibkr',
      currency: 'USD',
      nativeAmount: Number.isFinite(Number(ibkrPerf?.cash_base)) ? safeNum(ibkrPerf.cash_base) : ibkrCashCny,
      amountCny: ibkrCashCny,
      source: ibkrPerf?.source_file || 'ibkr_performance',
    }));
  }
  return rows;
}

export function shouldAggregateForMobile(row) {
  return holdingGroupLabel(row) === 'A股基金';
}

export function aggregateMobileHoldingRows(rows) {
  const buckets = new Map();
  const singles = [];
  for (const row of rows) {
    if (!shouldAggregateForMobile(row)) {
      singles.push(row);
      continue;
    }
    const key = `${holdingGroupLabel(row)}|${row.symbol || ''}|${row.name || ''}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(row);
  }

  const grouped = [];
  for (const groupRows of buckets.values()) {
    if (groupRows.length === 1) {
      grouped.push(groupRows[0]);
      continue;
    }
    const first = groupRows[0];
    const sum = (key) => groupRows.reduce((total, row) => total + safeNum(cnyValue(row, key)), 0);
    const sumRaw = (key) => groupRows.reduce((total, row) => total + safeNum(row[key]), 0);
    const quantity = groupRows.reduce((total, row) => total + safeNum(row.quantity), 0);
    const marketValue = sum('market_value');
    const cost = sum('cost');
    const holdingPnl = sum('holding_pnl');
    const dailyPnl = sum('daily_pnl');
    const dailyFxPnl = sum('daily_fx_pnl');
    const dailyTotalPnl = sum('daily_total_pnl') || dailyPnl + dailyFxPnl;
    const currentPrice = quantity > 0 ? marketValue / quantity : safeNum(first.current_price);
    const previousPrice = quantity > 0 ? (marketValue - dailyPnl) / quantity : safeNum(first.previous_price);
    const buyPrice = quantity > 0 ? cost / quantity : safeNum(first.buy_price);
    const dailyStatus = mergedDailyPnlStatus(groupRows);
    grouped.push({
      ...first,
      account: 'mixed',
      account_code: 'mixed',
      market_value: marketValue,
      market_value_cny: marketValue,
      market_value_native: sumRaw('market_value_native') || marketValue,
      market_value_gross_cny: sumRaw('market_value_gross_cny') || marketValue,
      cost,
      cost_cny: cost,
      cost_native: sumRaw('cost_native') || cost,
      cost_gross_cny: sumRaw('cost_gross_cny') || cost,
      holding_pnl: holdingPnl,
      holding_pnl_cny: holdingPnl,
      holding_pnl_native: sumRaw('holding_pnl_native') || holdingPnl,
      holding_pnl_gross_cny: sumRaw('holding_pnl_gross_cny') || holdingPnl,
      daily_pnl: dailyPnl,
      daily_pnl_cny: dailyPnl,
      daily_pnl_native: sumRaw('daily_pnl_native') || dailyPnl,
      daily_pnl_gross_cny: sumRaw('daily_pnl_gross_cny') || dailyPnl,
      daily_fx_pnl: dailyFxPnl,
      daily_fx_pnl_cny: dailyFxPnl,
      daily_total_pnl: dailyTotalPnl,
      daily_total_pnl_cny: dailyTotalPnl,
      quantity,
      buy_price: Number.isFinite(buyPrice) && buyPrice > 0 ? buyPrice : null,
      current_price: currentPrice,
      previous_price: Number.isFinite(previousPrice) ? previousPrice : null,
      price_change_pct: previousPrice ? (currentPrice - previousPrice) / previousPrice : null,
      price_change_amount: previousPrice ? currentPrice - previousPrice : null,
      daily_pnl_status: dailyStatus.status || first.daily_pnl_status || first.daily_return_status,
      daily_pnl_status_label: dailyStatus.label || first.daily_pnl_status_label,
      daily_return_status: dailyStatus.status || first.daily_return_status,
      daily_return_status_label: dailyStatus.label || first.daily_return_status_label,
      __isAggregated: true,
      __accountRows: groupRows,
    });
  }

  return [...singles, ...grouped].sort((a, b) => {
    const groupA = holdingGroupLabel(a);
    const groupB = holdingGroupLabel(b);
    return holdingGroupRank(groupA) - holdingGroupRank(groupB)
      || groupA.localeCompare(groupB, 'zh-CN')
      || safeNum(cnyValue(b, 'market_value')) - safeNum(cnyValue(a, 'market_value'));
  });
}

export function isInvestmentRow(row) {
  const category = row.major_category || '';
  if (['A股', '美股', '贵金属', 'ESOP'].includes(category)) return true;
  if (category === '现金') return true;
  return false;
}
