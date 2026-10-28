import { jget, typeLabel, accountLabel } from './api.js?v=20260619-investment-hardening-v1';
import { openBottomSheet, setActiveNav } from './ui.js?v=20261026-startup-order-v1';
import { pnlPct } from './utils.js?v=20260622-fund-pnl-v1';
import { makeAccountCashHoldingRows } from './account_cash.js?v=20260606-cash-active-v1';

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function cnyValue(r, key) {
  const cnyKey = `${key}_cny`;
  return r[cnyKey] ?? r[key] ?? 0;
}

function safeNum(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function roundMoneyCents(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.sign(n || 1) * Math.round((Math.abs(n) + 1e-9) * 100) / 100;
}

function subtractMoneyCents(left, right) {
  const leftNum = Number(left);
  const rightNum = Number(right);
  if (!Number.isFinite(leftNum) || !Number.isFinite(rightNum)) return null;
  return roundMoneyCents(leftNum - rightNum);
}

function formatNumber(value, digits = 2) {
  return safeNum(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatCurrency(value) {
  return `¥${formatNumber(value, 2)}`;
}

function formatCompactCurrency(value) {
  const n = safeNum(value);
  const sign = n < 0 ? '-' : '';
  return `${sign}¥${formatNumber(Math.abs(n), 2)}`;
}

function formatSignedCurrency(value) {
  const n = safeNum(value);
  return `${n >= 0 ? '+' : '-'}¥${formatNumber(Math.abs(n), 2)}`;
}

function signedMoneyClass(value) {
  return safeNum(value) >= 0 ? 'up' : 'down';
}

function formatNativeDaily(value, currency) {
  const ccy = String(currency || 'CNY').trim().toUpperCase();
  if (!ccy || ccy === 'CNY') return '';
  const n = safeNum(value);
  const abs = Math.abs(n);
  const digits = ccy === 'KRW' ? 0 : 2;
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}${formatNumber(abs, digits)} ${ccy}`;
}

function dailyFxText(row) {
  if (String(row?.currency || '').trim().toUpperCase() === 'CNY') return '';
  const value = row?.daily_fx_pnl_cny ?? row?.daily_fx_pnl;
  if (value == null || !Number.isFinite(Number(value))) return '证券汇兑 待核准';
  const fx = safeNum(value);
  if (Math.abs(fx) <= 0.005) return '';
  if (holdingDailyPnlPending(row)) return '证券汇兑 --';
  return `证券汇兑 ${formatSignedCurrency(fx)}`;
}

function dailyNativeText(row) {
  const native = formatNativeDaily(row.daily_pnl_native ?? row.daily_pnl, row.currency);
  if (!native) return '';
  return holdingDailyPnlPending(row) ? `-- ${String(row.currency).trim().toUpperCase()}` : native;
}

function holdingDailyPnlPending(row) {
  return row?.daily_pnl_stale === true
    || String(row?.daily_return_status || '').toLowerCase() === 'pending';
}

function hasDailyTotalPnl(row) {
  return row && (row.daily_total_pnl_cny != null || row.daily_total_pnl != null);
}

function datedReturnRow(row) {
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

function displayDailyPnl(row) {
  return hasDailyTotalPnl(row) ? safeNum(cnyValue(row, 'daily_total_pnl')) : safeNum(cnyValue(row, 'daily_pnl'));
}

function dailyPnlBreakdown(row) {
  const parts = [dailyNativeText(row), dailyFxText(row)].filter(Boolean);
  return parts.length ? parts.join(' · ') : '';
}

function dataBadge(row) {
  const category = row.major_category || row.type || '';
  if (category === 'ESOP') return '<span class="miniBadge pending">待核准</span>';
  if (category === '贵金属' || String(row.minor_category || '').includes('基金')) return '<span class="miniBadge warn">需核准</span>';
  if (!row.current_price && !row.buy_price && !row.quantity) return '<span class="miniBadge pending">净值型</span>';
  return '<span class="muted">—</span>';
}

function holdingDailyStatusBadge(row, options = {}) {
  if (holdingDailyPnlPending(row)) return '';
  const status = String(row?.daily_return_status || row?.daily_pnl_status || '').toLowerCase();
  if (!status || status === 'pending') return '';
  const label = row?.daily_return_status_label || row?.daily_pnl_status_label || (status === 'confirmed' ? '今日收益已确认' : status === 'estimated' ? '预估收益' : '');
  if (!label) return '';
  const tone = status === 'confirmed' ? 'ok' : 'warn';
  const extraClass = options.className ? ` ${esc(options.className)}` : '';
  return `<span class="miniBadge ${tone}${extraClass}"${row.__dailyReturnNote ? ` title="${esc(row.__dailyReturnNote)}"` : ''}>${esc(label)}</span>`;
}

function mergedDailyPnlStatus(rows) {
  if (rows.some(holdingDailyPnlPending)) return { status: 'pending', label: '' };
  const statuses = rows.map((row) => String(row?.daily_return_status || row?.daily_pnl_status || '').toLowerCase()).filter(Boolean);
  if (statuses.includes('estimated')) return { status: 'estimated', label: '预估收益' };
  if (statuses.includes('confirmed')) return { status: 'confirmed', label: '今日收益已确认' };
  return { status: '', label: '' };
}

function showClosedPositions() {
  return new URLSearchParams(window.location.search).get('show_closed') === '1';
}

function withClosedParam(path) {
  if (!showClosedPositions()) return path;
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}include_closed=true`;
}

function initClosedToggle() {
  const toggles = Array.from(document.querySelectorAll('.showClosedPositionsToggle'));
  const legacyToggle = document.getElementById('showClosedPositions');
  if (!toggles.length && legacyToggle) toggles.push(legacyToggle);
  if (!toggles.length) return;
  const checked = showClosedPositions();
  for (const el of toggles) {
    el.checked = checked;
  }
  for (const el of toggles) {
    el.addEventListener('change', () => {
      const nextChecked = el.checked;
      for (const other of toggles) {
        other.checked = nextChecked;
      }
      const url = new URL(window.location.href);
      if (nextChecked) {
        url.searchParams.set('show_closed', '1');
      } else {
        url.searchParams.delete('show_closed');
      }
      window.location.href = url.toString();
    });
  }
}

function closedBadge(r) {
  return r.is_closed ? '<span class="pill" style="margin-left:8px;padding:2px 6px">已平仓</span>' : '';
}

function displayPnlPct(r) {
  if (!r.is_closed) return pnlPct(r);
  const cost = Number(cnyValue(r, 'cost') || 0);
  return cost > 0 ? (Number(cnyValue(r, 'holding_pnl') || 0) / cost * 100) : 0;
}

function grossCnyValue(row, key) {
  const grossKey = `${key}_gross_cny`;
  if (Number.isFinite(Number(row[grossKey]))) return safeNum(row[grossKey]);
  const cnyKey = `${key}_cny`;
  if (Number.isFinite(Number(row[cnyKey]))) return safeNum(row[cnyKey]);
  return safeNum(row[key]);
}

function cumulativePnl(row) {
  return safeNum(cnyValue(row, 'holding_pnl'));
}

function currentFloatPnl(row) {
  return safeNum(cnyValue(row, 'market_value')) - safeNum(cnyValue(row, 'cost'));
}

function holdingCostValue(row) {
  return safeNum(cnyValue(row, 'cost'));
}

function realizedPnl(row) {
  if (Number.isFinite(Number(row.realized_trade_pnl_cny))) return safeNum(row.realized_trade_pnl_cny);
  return cumulativePnl(row) - currentFloatPnl(row);
}

function hasRealizedPnl(row) {
  return Math.abs(realizedPnl(row)) >= 0.005;
}

function realizedPnlNote(row) {
  if (!hasRealizedPnl(row)) return '—';
  return `含已实现 ${formatSignedCurrency(realizedPnl(row))}`;
}

function dilutedAveragePrice(row) {
  const quantity = Number(row.quantity);
  if (!Number.isFinite(quantity) || Math.abs(quantity) <= 0) return null;
  const cost = grossCnyValue(row, 'cost');
  const realized = grossCnyValue(row, 'realized_trade_pnl');
  if (cost > 0 && Math.abs(realized) >= 0.005) return (cost - realized) / quantity;
  return holdingAveragePrice(row);
}

function dilutedAveragePriceText(row) {
  const price = dilutedAveragePrice(row);
  if (!Number.isFinite(Number(price))) return '--';
  const text = formatNumber(price, holdingPriceDigits(row));
  const currency = String(row.currency || '').trim().toUpperCase();
  return currency && currency !== 'CNY' ? `${text} ${currency}` : text;
}

function holdingGroupLabel(row) {
  const label = typeLabel(row.minor_category || row.type || row.major_category) || '其他';
  const normalized = {
    A股: 'A股个股',
    美股基金: '美股/海外',
    黄金: '贵金属',
    员工持股: '内部股份/分红',
  };
  return normalized[label] || label;
}

function holdingGroupRank(label) {
  const order = ['A股个股', 'A股基金', '基金', '美股', '美股/海外', '贵金属', '内部股份', 'ESOP', '现金/活期', '现金'];
  const index = order.findIndex((item) => label.includes(item));
  return index >= 0 ? index : order.length;
}

function holdingGroupTone(label) {
  const text = String(label || '');
  if (text.includes('A股')) return 'cn';
  if (text.includes('美股') || text.includes('海外')) return 'global';
  if (text.includes('基金')) return 'fund';
  if (text.includes('贵金属') || text.includes('黄金')) return 'gold';
  if (text.includes('内部') || text.includes('ESOP')) return 'private';
  return 'other';
}

function renderRows(rows, options = {}){
  const tbody = document.getElementById('rows');
  tbody.innerHTML='';
  const totalRows = options.totalRows || rows;
  const total = totalRows.reduce((sum, row) => sum + safeNum(cnyValue(row, 'market_value')), 0);
  const showGroupRows = Boolean(options.showGroupRows);
  const displayRows = [...rows].sort((a, b) => {
    const groupA = holdingGroupLabel(a);
    const groupB = holdingGroupLabel(b);
    return holdingGroupRank(groupA) - holdingGroupRank(groupB)
      || groupA.localeCompare(groupB, 'zh-CN')
      || safeNum(cnyValue(b, 'market_value')) - safeNum(cnyValue(a, 'market_value'));
  });
  renderMobileHoldingRows(displayRows, { total });
  let lastGroup = '';
  for(const [index, r] of displayRows.entries()){
    const value = safeNum(cnyValue(r, 'market_value'));
    const floatPnl = currentFloatPnl(r);
    const totalPnl = cumulativePnl(r);
    const dailyPnl = displayDailyPnl(r);
    const dailyPending = holdingDailyPnlPending(r);
    const weight = total > 0 ? value / total * 100 : 0;
    const groupLabel = holdingGroupLabel(r);
    if (showGroupRows && groupLabel !== lastGroup) {
      const group = document.createElement('tr');
      group.className = 'holdingGroupRow';
      group.innerHTML = `<td colspan="11">${esc(groupLabel)}</td>`;
      tbody.appendChild(group);
      lastGroup = groupLabel;
    }
    const tr=document.createElement('tr');
    tr.className = 'holdingMainRow';
    tr.dataset.detailTarget = `holding-detail-${index}`;
    tr.title = '点击查看完整字段';
    tr.innerHTML = `
      <td class="nowrap">${esc(r.symbol || '—')}</td>
      <td class="name" title="${esc(r.name || '')}">${esc(r.name || '')}${closedBadge(r)}${holdingDailyStatusBadge(r)}</td>
      <td>${esc(groupLabel)}</td>
      <td>${esc(accountLabel(r.account || r.account_code || ''))}</td>
      <td class="mono num">${formatCompactCurrency(value)}</td>
      <td class="mono num">${holdingQuantityText(r)}</td>
      <td class="mono num">${r.current_price ? formatNumber(r.current_price, 3) : '价格缺失'}</td>
      <td class="mono num" style="color:${dailyPending ? 'var(--muted)' : dailyPnl>=0?'var(--good)':'var(--bad)'}">${dailyPending ? '待更新' : formatSignedCurrency(dailyPnl)}</td>
      <td class="mono num" style="color:${totalPnl>=0?'var(--good)':'var(--bad)'}">${formatSignedCurrency(totalPnl)}</td>
      <td class="mono">${realizedPnlNote(r)}</td>
      <td>${dataBadge(r)}</td>
    `;
    tbody.appendChild(tr);
    const detail = document.createElement('tr');
    detail.id = `holding-detail-${index}`;
    detail.className = 'rowDetail holdingDetailRow';
    detail.hidden = true;
    detail.innerHTML = `
      <td colspan="11">
        <div class="rowDetailGrid">
          <div><strong>类别</strong><span>${esc(typeLabel(r.minor_category || r.type || r.major_category) || '其他')}</span></div>
          <div><strong>账户</strong><span>${esc(accountLabel(r.account || r.account_code || ''))}</span></div>
          <div><strong>币种</strong><span>${esc(r.currency || 'CNY')}</span></div>
          <div><strong>持仓占比</strong><span class="mono">${weight.toFixed(1)}%</span></div>
          <div><strong>市值</strong><span class="mono">${formatCompactCurrency(cnyValue(r, 'market_value'))}</span></div>
          <div><strong>剩余成本</strong><span class="mono">${formatCompactCurrency(cnyValue(r, 'cost'))}</span></div>
          <div><strong>当前浮盈</strong><span class="mono ${signedMoneyClass(floatPnl)}">${formatSignedCurrency(floatPnl)}</span></div>
          <div><strong>已实现</strong><span class="mono ${signedMoneyClass(realizedPnl(r))}">${formatSignedCurrency(realizedPnl(r))}</span></div>
          <div><strong>累计盈亏</strong><span class="mono ${signedMoneyClass(totalPnl)}">${formatSignedCurrency(totalPnl)}</span></div>
          <div><strong>今日盈亏</strong><span class="mono ${dailyPending ? '' : signedMoneyClass(dailyPnl)}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '待更新' : formatSignedCurrency(dailyPnl)}</span></div>
          <div><strong>收益状态</strong><span>${dailyPending ? '待更新' : holdingDailyStatusBadge(r) || '<span class="muted">—</span>'}</span></div>
          <div><strong>份数</strong><span class="mono">${esc(holdingQuantityText(r))}</span></div>
          <div><strong>剩余成本均价</strong><span class="mono">${esc(holdingAveragePriceText(r))}</span></div>
          <div><strong>摊薄成本</strong><span class="mono">${esc(dilutedAveragePriceText(r))}</span></div>
          <div><strong>现价</strong><span class="mono">${esc(holdingPriceText(r))}</span></div>
          <div><strong>数据</strong><span>${dataBadge(r)}</span></div>
          <div class="rowDetailFull"><strong>累计公式</strong><span class="mono">${formatSignedCurrency(floatPnl)} + ${formatSignedCurrency(realizedPnl(r))} = ${formatSignedCurrency(totalPnl)}</span></div>
        </div>
      </td>
    `;
    tbody.appendChild(detail);
    tr.addEventListener('click', () => {
      if (window.matchMedia('(max-width: 760px)').matches) {
        openBottomSheet(r.name || r.symbol || '持仓详情', detail.querySelector('td')?.innerHTML || detail.innerHTML);
        return;
      }
      detail.hidden = !detail.hidden;
      tr.classList.toggle('open', !detail.hidden);
    });
  }
}

function shortHoldingName(row) {
  const raw = String(row.name || row.symbol || '未命名资产');
  const symbol = String(row.symbol || '').trim();
  if (symbol && raw.startsWith(`${symbol} - `)) return raw.slice(symbol.length + 3);
  return raw;
}

function holdingPriceDigits(row) {
  const label = holdingGroupLabel(row);
  if (label.includes('基金') || label.includes('贵金属')) return 4;
  return 2;
}

function holdingPriceText(row) {
  if (!Number.isFinite(Number(row.current_price))) return '--';
  const price = formatNumber(row.current_price, holdingPriceDigits(row));
  const label = holdingGroupLabel(row);
  const currency = String(row.currency || '').trim().toUpperCase();
  if (currency && currency !== 'CNY' && (label.includes('美股') || String(row.major_category || '').includes('美股'))) {
    return `${price} ${currency}`;
  }
  return price;
}

function holdingQuantityText(row) {
  const quantity = Number(row.quantity);
  if (!Number.isFinite(quantity) || Math.abs(quantity) <= 0) return '--';
  return formatNumber(quantity, 3).replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1');
}

function holdingAveragePrice(row) {
  const direct = Number(row.buy_price);
  if (Number.isFinite(direct) && direct > 0) return direct;
  const quantity = Number(row.quantity);
  const cost = Number(row.cost);
  if (Number.isFinite(quantity) && quantity > 0 && Number.isFinite(cost) && cost > 0) {
    return cost / quantity;
  }
  return null;
}

function holdingAveragePriceText(row) {
  const price = holdingAveragePrice(row);
  if (!Number.isFinite(Number(price))) return '--';
  const text = formatNumber(price, holdingPriceDigits(row));
  const currency = String(row.currency || '').trim().toUpperCase();
  return currency && currency !== 'CNY' ? `${text} ${currency}` : text;
}

function holdingPriceChangePct(row) {
  const direct = Number(row.price_change_pct);
  if (Number.isFinite(direct)) return direct * 100;
  const daily = safeNum(cnyValue(row, 'daily_pnl'));
  const value = safeNum(cnyValue(row, 'market_value'));
  const base = value - daily;
  if (Math.abs(daily) > 0.005 && Math.abs(base) > 0.005) return daily / base * 100;
  return null;
}

function signedPercentText(value) {
  if (!Number.isFinite(Number(value))) return '--';
  const n = Number(value);
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

function cashRow({ symbol, name, account, currency = 'CNY', nativeAmount = 0, amountCny = null, source = 'account' }) {
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

function buildAccountCashRows(accountOverview, fxRates = {}, ibkrPerf = null) {
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

function shouldAggregateForMobile(row) {
  return holdingGroupLabel(row) === 'A股基金';
}

function aggregateMobileHoldingRows(rows) {
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

function holdingSyncTime(row) {
  const rows = row.__accountRows || [row];
  const dates = rows.map(item => {
    const raw = String(item.valuation_updated_at || '');
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(raw)) return null;
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? null : date;
  });
  if (!dates.length || dates.some(date => !date)) {
    return '<span class="mobileHoldingSync">时间未知</span>';
  }
  // A merged fund must not look fresher than its oldest account record.
  const date = new Date(Math.min(...dates.map(item => item.getTime())));
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).reduce((acc, part) => ({ ...acc, [part.type]: part.value }), {});
  const short = `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  const full = `${parts.year}-${short} 北京时间；估值记录同步时间，不代表收益确认日期${rows.length > 1 ? '；多账户取最早同步时间' : ''}`;
  return `<span class="mobileHoldingSync" title="${esc(full)}"><time datetime="${date.toISOString()}" aria-label="${esc(full)}">${short}</time></span>`;
}

function renderMobileHoldingRows(rows, options = {}) {
  const el = document.getElementById('mobileHoldingList');
  if (!el) return;
  const total = options.total || rows.reduce((sum, row) => sum + safeNum(cnyValue(row, 'market_value')), 0);
  const displayRows = aggregateMobileHoldingRows(rows);
  if (!rows.length) {
    el.innerHTML = '<div class="mobileEmpty">暂无持仓</div>';
    return;
  }
  el.innerHTML = displayRows.map((raw, index) => {
    const r = datedReturnRow(raw);
    const value = safeNum(cnyValue(r, 'market_value'));
    const costValue = holdingCostValue(r);
    const floatPnl = currentFloatPnl(r);
    const totalPnl = cumulativePnl(r);
    const dailyPnl = displayDailyPnl(r);
    const dailyPending = holdingDailyPnlPending(r);
    const dailyBreakdown = dailyPnlBreakdown(r);
    const changePct = holdingPriceChangePct(r);
    const name = shortHoldingName(r);
    const isCash = holdingGroupLabel(r) === '现金/活期';
    const cashNative = safeNum(r.market_value_native ?? r.market_value);
    const quoteSub = isCash
      ? (String(r.currency || 'CNY').toUpperCase() === 'CNY' ? '' : formatNumber(cashNative, 2))
      : holdingPriceText(r);
    const quoteTone = isCash ? (value < 0 ? 'down' : '') : (Number(changePct) >= 0 ? 'up' : 'down');
    const quantityText = holdingQuantityText(r);
    const quoteItems = [
      quantityText === '--' ? '' : `<span><em>份数</em><strong class="mono">${esc(quantityText)}</strong></span>`,
      quoteSub ? `<span><em>${isCash ? '本币' : '现价'}</em><strong class="mono">${esc(quoteSub)}</strong></span>` : '',
    ].filter(Boolean).join('<i aria-hidden="true">·</i>');
    const quoteLine = quoteItems ? `<span class="mobileHoldingQuoteLine">${quoteItems}</span>` : '';
    const dailyChange = isCash ? '' : `<small class="mobileHoldingChangePct mono ${dailyPending ? '' : quoteTone}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '--' : signedPercentText(changePct)}</small>`;
    return `
      <button class="mobileHoldingRow" type="button" data-mobile-holding-index="${index}" title="${esc(r.name || name)}">
        <span class="mobileHoldingMain">
          <span class="mobileHoldingText">
            <strong><span class="mobileHoldingNameInline">${esc(name)}</span>${r.is_closed ? '<em>已平仓</em>' : ''}</strong>
            <small>${esc(holdingGroupLabel(r))}</small>
          </span>
        </span>
        <span class="mobileHoldingMeta">
          ${holdingDailyStatusBadge(r)}
          <span class="mobileHoldingQuote">${quoteLine}</span>
          ${holdingSyncTime(r)}
        </span>
        <span class="mobileHoldingMetrics">
          <span class="mobileHoldingMetric">
            <em>持仓成本</em>
            <strong class="mono">${formatCompactCurrency(costValue)}</strong>
          </span>
          <span class="mobileHoldingMetric">
            <em>累计盈亏</em>
            <strong class="mono ${signedMoneyClass(totalPnl)}">${formatSignedCurrency(totalPnl)}</strong>
            ${hasRealizedPnl(r) ? `<small class="mobileHoldingFxImpact">${realizedPnlNote(r)}</small>` : ''}
          </span>
          <span class="mobileHoldingMetric">
            <em${r.__dailyReturnNote ? ` title="${esc(r.__dailyReturnNote)}"` : ''}>${esc(r.__dailyReturnLabel || '今日盈亏')}</em>
            <span class="mobileHoldingDailyLine">
              <strong class="mono ${dailyPending ? '' : signedMoneyClass(dailyPnl)}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '待更新' : formatSignedCurrency(dailyPnl)}</strong>
              ${dailyChange}
            </span>
            ${dailyBreakdown ? `<small class="mobileHoldingFxImpact">${esc(dailyBreakdown)}</small>` : ''}
          </span>
        </span>
      </button>
    `;
  }).join('');
  el.querySelectorAll('[data-mobile-holding-index]').forEach((node) => {
    node.addEventListener('click', () => {
      const row = datedReturnRow(displayRows[Number(node.dataset.mobileHoldingIndex)]);
      const value = safeNum(cnyValue(row, 'market_value'));
      const weight = total > 0 ? value / total * 100 : 0;
      const changePct = holdingPriceChangePct(row);
      const floatPnl = currentFloatPnl(row);
      const realized = realizedPnl(row);
      const totalPnl = cumulativePnl(row);
      const dailyPnl = displayDailyPnl(row);
      const dailyPending = holdingDailyPnlPending(row);
      const priceDailyPnl = safeNum(cnyValue(row, 'daily_pnl'));
      const detailNativeDaily = dailyNativeText(row);
      const detailFxDaily = dailyFxText(row);
      const accountBreakdown = Array.isArray(row.__accountRows) && row.__accountRows.length > 1
        ? `
          <div class="rowDetailFull">
            <strong>账户拆分</strong>
            <span>${row.__accountRows.map((item) => `${esc(accountLabel(item.account || item.account_code || ''))}: ${formatCompactCurrency(cnyValue(item, 'market_value'))}`).join(' / ')}</span>
          </div>
        `
        : '';
      const html = `
        <div class="rowDetailGrid">
          <div><strong>名称</strong><span>${esc(row.name || row.symbol || '缺数据')}</span></div>
          <div><strong>类别</strong><span>${esc(holdingGroupLabel(row))}</span></div>
          <div><strong>持仓占比</strong><span class="mono">${weight.toFixed(1)}%</span></div>
          <div><strong>账户</strong><span>${esc(accountLabel(row.account || row.account_code || '') || '缺数据')}</span></div>
          <div><strong>币种</strong><span>${esc(row.currency || 'CNY')}</span></div>
          <div><strong>市值</strong><span class="mono">${formatCompactCurrency(value)}</span></div>
          <div><strong>剩余成本</strong><span class="mono">${formatCompactCurrency(cnyValue(row, 'cost'))}</span></div>
          <div><strong>当前浮盈</strong><span class="mono ${signedMoneyClass(floatPnl)}">${formatSignedCurrency(floatPnl)}</span></div>
          <div><strong>已实现</strong><span class="mono ${signedMoneyClass(realized)}">${formatSignedCurrency(realized)}</span></div>
          <div><strong>累计盈亏</strong><span class="mono ${signedMoneyClass(totalPnl)}">${formatSignedCurrency(totalPnl)}</span></div>
          <div><strong>${esc(row.__dailyReturnLabel || '今日盈亏')}</strong><span class="mono ${dailyPending ? '' : signedMoneyClass(dailyPnl)}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '待更新' : formatSignedCurrency(dailyPnl)}</span></div>
          <div><strong>收益状态</strong><span>${dailyPending ? '待更新' : holdingDailyStatusBadge(row) || '<span class="muted">—</span>'}</span></div>
          ${hasDailyTotalPnl(row) ? `<div><strong>价格影响</strong><span class="mono ${dailyPending ? '' : signedMoneyClass(priceDailyPnl)}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '--' : formatSignedCurrency(priceDailyPnl)}</span></div>` : ''}
          ${detailNativeDaily ? `<div><strong>${row.__dailyReturnDate ? `本币${esc(row.__dailyReturnDate.slice(5).replace('-', '/'))}` : '本币今日'}</strong><span class="mono">${esc(detailNativeDaily)}</span></div>` : ''}
          ${detailFxDaily ? `<div><strong>证券汇兑估算</strong><span class="mono ${dailyPending ? '' : signedMoneyClass(cnyValue(row, 'daily_fx_pnl'))}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${esc(detailFxDaily.replace(/^证券汇兑\s*/, ''))}</span></div>` : ''}
          <div><strong>份数</strong><span class="mono">${esc(holdingQuantityText(row))}</span></div>
          <div><strong>剩余成本均价</strong><span class="mono">${esc(holdingAveragePriceText(row))}</span></div>
          <div><strong>摊薄成本</strong><span class="mono">${esc(dilutedAveragePriceText(row))}</span></div>
          <div><strong>现价</strong><span class="mono">${esc(holdingPriceText(row))}</span></div>
          <div><strong>涨跌幅</strong><span class="mono ${dailyPending ? '' : Number(changePct) >= 0 ? 'up' : 'down'}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '--' : signedPercentText(changePct)}</span></div>
          <div><strong>数据</strong><span>${dataBadge(row)}</span></div>
          <div class="rowDetailFull"><strong>累计公式</strong><span class="mono">${formatSignedCurrency(floatPnl)} + ${formatSignedCurrency(realized)} = ${formatSignedCurrency(totalPnl)}</span></div>
          ${accountBreakdown}
        </div>
      `;
      openBottomSheet(shortHoldingName(row), html);
    });
  });
}

function isInvestmentRow(row) {
  const category = row.major_category || '';
  if (['A股', '美股', '贵金属', 'ESOP'].includes(category)) return true;
  if (category === '现金') return true;
  return false;
}

function setupHoldingGroupFilter(allRows) {
  const sel = document.getElementById('holdingGroupFilter');
  const chips = document.getElementById('mobileHoldingFilterChips');
  if (!sel) {
    renderRows(allRows, { totalRows: allRows, showGroupRows: true });
    return;
  }
  const groups = Array.from(new Set(allRows.map(holdingGroupLabel)))
    .sort((a, b) => holdingGroupRank(a) - holdingGroupRank(b) || a.localeCompare(b, 'zh-CN'));
  sel.innerHTML = '';
  const allLabel = '全部';
  let selectedGroup = allLabel;
  const allOpt = document.createElement('option');
  allOpt.value = allLabel;
  allOpt.textContent = allLabel;
  sel.appendChild(allOpt);
  for (const group of groups) {
    const opt = document.createElement('option');
    opt.value = group;
    opt.textContent = group;
    sel.appendChild(opt);
  }
  sel.value = selectedGroup;
  const renderChips = () => {
    if (!chips) return;
    chips.innerHTML = [allLabel, ...groups].map((label) => `
      <button class="mobileFilterChip ${label === selectedGroup ? 'active' : ''}" type="button" data-group="${esc(label)}">${esc(label)}</button>
    `).join('');
    chips.querySelectorAll('[data-group]').forEach((btn) => {
      btn.addEventListener('click', () => {
        selectedGroup = btn.dataset.group || allLabel;
        if (selectedGroup !== allLabel && groups.includes(selectedGroup)) sel.value = selectedGroup;
        apply();
      });
    });
  };
  const apply = () => {
    const selected = selectedGroup === allLabel ? allLabel : sel.value;
    selectedGroup = selected || allLabel;
    const rows = selectedGroup === allLabel ? allRows : allRows.filter((row) => holdingGroupLabel(row) === selectedGroup);
    renderRows(rows, { totalRows: allRows, showGroupRows: false });
    renderChips();
    window.dispatchEvent(new CustomEvent('pams:holding-group-change', { detail: { group: selectedGroup } }));
  };
  sel.addEventListener('change', () => {
    selectedGroup = sel.value || allLabel;
    apply();
  });
  apply();
}

async function main(){
  setActiveNav();
  initClosedToggle();
  const [holdings, accountOverview, fxData, ibkrPerf] = await Promise.all([
    jget(withClosedParam('/holdings/latest')),
    jget('/api/accounts/overview').catch(() => null),
    jget('/api/fx-rates/latest?base_currency=CNY').catch(() => null),
    jget('/api/ibkr/performance').catch(() => null),
  ]);
  document.getElementById('asof').textContent = holdings.as_of || '--';

  const fxRates = { CNY: 1 };
  for (const r of fxData?.rates || []) {
    fxRates[r.quote_currency] = Number(r.rate);
  }

  const sourceRows = (holdings.rows || [])
    .filter(isInvestmentRow)
    .filter((row) => !(row.major_category === '现金' && row.account && row.account !== 'tonghuashun'));
  const accountCashRows = buildAccountCashRows(accountOverview, fxRates, ibkrPerf);
  const allRows = [...sourceRows, ...accountCashRows];

  setupHoldingGroupFilter(allRows);
}

main().catch(err=>{
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
