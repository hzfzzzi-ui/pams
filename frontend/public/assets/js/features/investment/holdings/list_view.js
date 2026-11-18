import {
  esc, cnyValue, safeNum, formatNumber,
  formatCompactCurrency, formatSignedCurrency, signedMoneyClass, dailyFxText,
  dailyNativeText, holdingDailyPnlPending, hasDailyTotalPnl, datedReturnRow,
  displayDailyPnl, dailyPnlBreakdown, cumulativePnl, currentFloatPnl,
  holdingCostValue, realizedPnl, realizedPnlNote,
  dilutedAveragePriceText, holdingGroupLabel, holdingGroupRank, shortHoldingName,
  holdingPriceText, holdingQuantityText, holdingAveragePriceText, holdingPriceChangePct,
  signedPercentText, aggregateMobileHoldingRows
} from './list_legacy_model.js';
import { typeLabel, accountLabel } from '../../../api.js';
import { bindMarketDetail, marketDetailHtml } from './market_detail.js';

export function createListView({ document, host: window, showDetails: openBottomSheet, scope }) {
  const rowListeners = new Set();
  function clearRows() { for (const remove of rowListeners) remove(); rowListeners.clear(); }
  function bindRow(node, type, callback) { rowListeners.add(scope.listen(node, type, callback)); }
function dataBadge(row) {
  if (row.reference_as_of) return `<span class="miniBadge pending">历史参考 · ${esc(row.reference_as_of)}</span>`;
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

function closedBadge(r) {
  return r.is_closed ? '<span class="pill" style="margin-left:8px;padding:2px 6px">已平仓</span>' : '';
}

function cumulativeFormula(row, floatPnl, realized, total) {
  const hasDistributions = row.net_distributions_cny != null || row.net_distributions != null;
  const income = hasDistributions ? ` + 分红税后 ${formatSignedCurrency(cnyValue(row, 'net_distributions'))}` : '';
  return `浮盈 ${formatSignedCurrency(floatPnl)} + 已实现 ${formatSignedCurrency(realized)}${income} = ${formatSignedCurrency(total)}`;
}

function dilutedCostLabel(row) {
  return row.diluted_cost_source === 'verified_trade_snapshot' ? '同花顺摊薄成本（流水计算）' : '摊薄成本（估算）';
}

function snapshotQuoteDate(row) {
  return row.diluted_cost_source === 'verified_trade_snapshot' && row.snapshot_as_of
    ? String(row.snapshot_as_of).slice(0, 10) : '';
}

function holdingMetrics(row, total) {
  const value = safeNum(cnyValue(row, 'market_value'));
  const historical = Boolean(row.reference_as_of);
  return {
    value,
    weight: historical || total == null ? '—' : `${(total > 0 ? value / total * 100 : 0).toFixed(1)}%`,
    valueLabel: historical ? '参考市值' : '市值',
    costValue: historical ? null : holdingCostValue(row),
    floatPnl: historical ? null : currentFloatPnl(row),
    realized: historical ? null : realizedPnl(row),
    totalPnl: historical ? null : cumulativePnl(row),
    dailyPnl: historical ? null : displayDailyPnl(row),
    dailyPending: !historical && holdingDailyPnlPending(row),
  };
}

function pnlText(value) {
  return value == null ? '—' : formatSignedCurrency(value);
}

function pnlTone(value) {
  return value == null ? 'muted' : signedMoneyClass(value);
}

function holdingAccountName(row) {
  return row.account_name || accountLabel(row.account || row.account_code || '');
}

function openHoldingDetail(row, total, fields) {
  const title = shortHoldingName(row);
  if (row.reference_as_of || holdingGroupLabel(row) === '现金/活期') {
    openBottomSheet(title, fields);
    return;
  }
  const metrics = holdingMetrics(row, total);
  const accounts = [...new Set((row.__accountRows || [row]).map(holdingAccountName))];
  const body = openBottomSheet(title, marketDetailHtml({
    subtitle: [row.symbol, holdingGroupLabel(row), accounts.join(' / ')].filter(Boolean).join(' · '),
    value: formatCompactCurrency(metrics.value), totalPnl: pnlText(metrics.totalPnl),
    pnlClass: pnlTone(metrics.totalPnl),
    breakdown: [['当前浮盈',metrics.floatPnl],['已实现盈亏',metrics.realized]].map(([label, n]) =>
      ({label, amount:pnlText(n), tone:pnlTone(n)})), fields,
  }));
  bindMarketDetail(body?.querySelector('.holdingMarketDetail'), row);
}

function renderRows(rows, options = {}){
  clearRows();
  const tbody = document.getElementById('rows');
  tbody.innerHTML='';
  const totalRows = options.totalRows || rows;
  const total = options.cashPending ? null : totalRows.reduce((sum, row) => sum + safeNum(cnyValue(row, 'market_value')), 0);
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
  for(const r of displayRows){
    const { value, weight, valueLabel, costValue, floatPnl, realized, totalPnl, dailyPnl, dailyPending } = holdingMetrics(r, total);
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
    tr.title = '点击查看完整字段';
    tr.innerHTML = `
      <td class="nowrap">${esc(r.symbol || '—')}</td>
      <td class="name" title="${esc(r.name || '')}">${esc(r.name || '')}${closedBadge(r)}${holdingDailyStatusBadge(r)}</td>
      <td>${esc(groupLabel)}</td>
      <td>${esc(holdingAccountName(r))}</td>
      <td class="mono num">${formatCompactCurrency(value)}</td>
      <td class="mono num">${holdingQuantityText(r)}</td>
      <td class="mono num">${r.current_price ? formatNumber(r.current_price, 3) : '价格缺失'}</td>
      <td class="mono num ${dailyPending ? 'muted' : pnlTone(dailyPnl)}">${dailyPending ? '待更新' : pnlText(dailyPnl)}</td>
      <td class="mono num ${pnlTone(totalPnl)}">${pnlText(totalPnl)}</td>
      <td class="mono">${realizedPnlNote(realized) || '—'}</td>
      <td>${dataBadge(r)}</td>
    `;
    tbody.appendChild(tr);
    const fields = `
        <div class="rowDetailGrid">
          <div><strong>类别</strong><span>${esc(typeLabel(r.minor_category || r.type || r.major_category) || '其他')}</span></div>
          <div><strong>账户</strong><span>${esc(holdingAccountName(r))}</span></div>
          <div><strong>币种</strong><span>${esc(r.currency || 'CNY')}</span></div>
          <div><strong>持仓占比</strong><span class="mono">${weight}</span></div>
          <div><strong>${valueLabel}</strong><span class="mono">${formatCompactCurrency(value)}</span></div>
          <div><strong>剩余成本</strong><span class="mono">${costValue == null ? '—' : formatCompactCurrency(costValue)}</span></div>
          <div><strong>当前浮盈</strong><span class="mono ${pnlTone(floatPnl)}">${pnlText(floatPnl)}</span></div>
          <div><strong>已实现</strong><span class="mono ${pnlTone(realized)}">${pnlText(realized)}</span></div>
          <div><strong>累计盈亏</strong><span class="mono ${pnlTone(totalPnl)}">${pnlText(totalPnl)}</span></div>
          <div><strong>今日盈亏</strong><span class="mono ${dailyPending ? 'muted' : pnlTone(dailyPnl)}">${dailyPending ? '待更新' : pnlText(dailyPnl)}</span></div>
          <div><strong>收益状态</strong><span>${dailyPending ? '待更新' : holdingDailyStatusBadge(r) || '<span class="muted">—</span>'}</span></div>
          <div><strong>份数</strong><span class="mono">${esc(holdingQuantityText(r))}</span></div>
          <div><strong>剩余成本均价</strong><span class="mono">${esc(holdingAveragePriceText(r))}</span></div>
          <div><strong>${dilutedCostLabel(r)}</strong><span class="mono">${esc(dilutedAveragePriceText(r))}</span></div>
          <div><strong>现价</strong><span class="mono">${esc(holdingPriceText(r))}</span></div>
          <div><strong>数据</strong><span>${dataBadge(r)}</span></div>
          ${snapshotQuoteDate(r) ? `<div><strong>行情日期</strong><span>${esc(snapshotQuoteDate(r))}</span></div>` : ''}
          ${totalPnl == null ? '' : `<div class="rowDetailFull"><strong>累计公式</strong><span class="mono">${cumulativeFormula(r, floatPnl, realized, totalPnl)}</span></div>`}
        </div>
    `;
    bindRow(tr, 'click', () => openHoldingDetail(r, total, fields));
  }
}

function holdingSyncTime(row) {
  if (row.reference_as_of) {
    const date = esc(row.reference_as_of);
    return `<span class="mobileHoldingSync">历史参考 · <time datetime="${date}">${date}</time></span>`;
  }
  const rows = row.__accountRows || [row];
  const balanceDate = rows.length === 1 ? String(rows[0].balance_as_of || '') : '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(balanceDate)) {
    return `<span class="mobileHoldingSync">余额日 <time datetime="${balanceDate}">${balanceDate.slice(5)}</time></span>`;
  }
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

function renderMobileHoldingRows(rows, { total }) {
  const el = document.getElementById('mobileHoldingList');
  if (!el) return;

  const displayRows = aggregateMobileHoldingRows(rows);
  if (!rows.length) {
    el.innerHTML = '<div class="mobileEmpty">暂无持仓</div>';
    return;
  }
  el.innerHTML = displayRows.map((raw, index) => {
    const r = datedReturnRow(raw);
    const { value, valueLabel, costValue, realized, totalPnl, dailyPnl, dailyPending } = holdingMetrics(r, total);
    const realizedNote = realizedPnlNote(realized);
    const dailyBreakdown = dailyPnlBreakdown(r);
    const changePct = holdingPriceChangePct(r);
    const name = shortHoldingName(r);
    const isCash = holdingGroupLabel(r) === '现金/活期';
    const cashNative = safeNum(r.market_value_native ?? r.market_value);
    const quoteSub = r.reference_as_of ? formatCompactCurrency(value) : isCash
      ? (String(r.currency || 'CNY').toUpperCase() === 'CNY' ? '' : formatNumber(cashNative, 2))
      : holdingPriceText(r);
    const quoteTone = isCash ? (value < 0 ? 'down' : '') : (Number(changePct) >= 0 ? 'up' : 'down');
    const quantityText = holdingQuantityText(r);
    const quoteItems = [
      quantityText === '--' ? '' : `<span><em>份数</em><strong class="mono">${esc(quantityText)}</strong></span>`,
      quoteSub ? `<span><em>${r.reference_as_of ? valueLabel : isCash ? '本币' : '现价'}</em><strong class="mono">${esc(quoteSub)}</strong></span>` : '',
    ].filter(Boolean).join('<i aria-hidden="true">·</i>');
    const quoteLine = quoteItems ? `<span class="mobileHoldingQuoteLine">${quoteItems}</span>` : '';
    const dailyChange = isCash || dailyPnl == null ? '' : `<small class="mobileHoldingChangePct mono ${dailyPending ? '' : quoteTone}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '--' : signedPercentText(changePct)}</small>`;
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
          ${snapshotQuoteDate(r) ? `<span class="mobileHoldingSync">行情 ${esc(snapshotQuoteDate(r))}</span>` : ''}
        </span>
        <span class="mobileHoldingMetrics">
          <span class="mobileHoldingMetric">
            <em>持仓成本</em>
            <strong class="mono">${costValue == null ? '—' : formatCompactCurrency(costValue)}</strong>
          </span>
          <span class="mobileHoldingMetric">
            <em>累计盈亏</em>
            <strong class="mono ${pnlTone(totalPnl)}">${pnlText(totalPnl)}</strong>
            ${realizedNote ? `<small class="mobileHoldingFxImpact">${realizedNote}</small>` : ''}
          </span>
          <span class="mobileHoldingMetric">
            <em${r.__dailyReturnNote ? ` title="${esc(r.__dailyReturnNote)}"` : ''}>${esc(r.__dailyReturnLabel || '今日盈亏')}</em>
            <span class="mobileHoldingDailyLine">
              <strong class="mono ${dailyPending ? 'muted' : pnlTone(dailyPnl)}">${dailyPending ? '待更新' : pnlText(dailyPnl)}</strong>
              ${dailyChange}
            </span>
            ${dailyBreakdown ? `<small class="mobileHoldingFxImpact">${esc(dailyBreakdown)}</small>` : ''}
          </span>
        </span>
      </button>
    `;
  }).join('');
  el.querySelectorAll('[data-mobile-holding-index]').forEach((node) => {
    bindRow(node, 'click', () => {
      const row = datedReturnRow(displayRows[Number(node.dataset.mobileHoldingIndex)]);
      const { value, weight, valueLabel, costValue, floatPnl, realized, totalPnl, dailyPnl, dailyPending } = holdingMetrics(row, total);
      const changePct = holdingPriceChangePct(row);
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
          <div><strong>持仓占比</strong><span class="mono">${weight}</span></div>
          <div><strong>账户</strong><span>${esc(holdingAccountName(row) || '缺数据')}</span></div>
          <div><strong>币种</strong><span>${esc(row.currency || 'CNY')}</span></div>
          <div><strong>${valueLabel}</strong><span class="mono">${formatCompactCurrency(value)}</span></div>
          <div><strong>剩余成本</strong><span class="mono">${costValue == null ? '—' : formatCompactCurrency(costValue)}</span></div>
          <div><strong>当前浮盈</strong><span class="mono ${pnlTone(floatPnl)}">${pnlText(floatPnl)}</span></div>
          <div><strong>已实现</strong><span class="mono ${pnlTone(realized)}">${pnlText(realized)}</span></div>
          <div><strong>累计盈亏</strong><span class="mono ${pnlTone(totalPnl)}">${pnlText(totalPnl)}</span></div>
          <div><strong>${esc(row.__dailyReturnLabel || '今日盈亏')}</strong><span class="mono ${dailyPending ? 'muted' : pnlTone(dailyPnl)}">${dailyPending ? '待更新' : pnlText(dailyPnl)}</span></div>
          <div><strong>收益状态</strong><span>${dailyPending ? '待更新' : holdingDailyStatusBadge(row) || '<span class="muted">—</span>'}</span></div>
          ${hasDailyTotalPnl(row) ? `<div><strong>价格影响</strong><span class="mono ${dailyPending ? '' : signedMoneyClass(priceDailyPnl)}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '--' : formatSignedCurrency(priceDailyPnl)}</span></div>` : ''}
          ${detailNativeDaily ? `<div><strong>${row.__dailyReturnDate ? `本币${esc(row.__dailyReturnDate.slice(5).replace('-', '/'))}` : '本币今日'}</strong><span class="mono">${esc(detailNativeDaily)}</span></div>` : ''}
          ${detailFxDaily ? `<div><strong>证券汇兑估算</strong><span class="mono ${dailyPending ? '' : signedMoneyClass(cnyValue(row, 'daily_fx_pnl'))}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${esc(detailFxDaily.replace(/^证券汇兑\s*/, ''))}</span></div>` : ''}
          <div><strong>份数</strong><span class="mono">${esc(holdingQuantityText(row))}</span></div>
          <div><strong>剩余成本均价</strong><span class="mono">${esc(holdingAveragePriceText(row))}</span></div>
          <div><strong>${dilutedCostLabel(row)}</strong><span class="mono">${esc(dilutedAveragePriceText(row))}</span></div>
          <div><strong>现价</strong><span class="mono">${esc(holdingPriceText(row))}</span></div>
          <div><strong>涨跌幅</strong><span class="mono ${dailyPending ? '' : Number(changePct) >= 0 ? 'up' : 'down'}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '--' : signedPercentText(changePct)}</span></div>
          <div><strong>数据</strong><span>${dataBadge(row)}</span></div>
          ${snapshotQuoteDate(row) ? `<div><strong>行情日期</strong><span>${esc(snapshotQuoteDate(row))}</span></div>` : ''}
          ${totalPnl == null ? '' : `<div class="rowDetailFull"><strong>累计公式</strong><span class="mono">${cumulativeFormula(row, floatPnl, realized, totalPnl)}</span></div>`}
          ${accountBreakdown}
        </div>
      `;
      openHoldingDetail(row, total, html);
    });
  });
}

return { dataBadge, holdingDailyStatusBadge, closedBadge, renderRows, holdingSyncTime, renderMobileHoldingRows, dispose: clearRows };
}
