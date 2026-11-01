import {
  esc, cnyValue, safeNum, formatNumber,
  formatCompactCurrency, formatSignedCurrency, signedMoneyClass, dailyFxText,
  dailyNativeText, holdingDailyPnlPending, hasDailyTotalPnl, datedReturnRow,
  displayDailyPnl, dailyPnlBreakdown, cumulativePnl, currentFloatPnl,
  holdingCostValue, realizedPnl, hasRealizedPnl, realizedPnlNote,
  dilutedAveragePriceText, holdingGroupLabel, holdingGroupRank, shortHoldingName,
  holdingPriceText, holdingQuantityText, holdingAveragePriceText, holdingPriceChangePct,
  signedPercentText, aggregateMobileHoldingRows
} from './list_legacy_model.js?v=20261102-verified-trade-snapshot-v1';
import { typeLabel, accountLabel } from '../../../api.js?v=20261031-modules-v1';

export function createListView({ document, host: window, showDetails: openBottomSheet, scope }) {
  const rowListeners = new Set();
  function clearRows() { for (const remove of rowListeners) remove(); rowListeners.clear(); }
  function bindRow(node, type, callback) { rowListeners.add(scope.listen(node, type, callback)); }
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

function renderRows(rows, options = {}){
  clearRows();
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
          <div><strong>${dilutedCostLabel(r)}</strong><span class="mono">${esc(dilutedAveragePriceText(r))}</span></div>
          <div><strong>现价</strong><span class="mono">${esc(holdingPriceText(r))}</span></div>
          <div><strong>数据</strong><span>${dataBadge(r)}</span></div>
          ${snapshotQuoteDate(r) ? `<div><strong>行情日期</strong><span>${esc(snapshotQuoteDate(r))}</span></div>` : ''}
          <div class="rowDetailFull"><strong>累计公式</strong><span class="mono">${cumulativeFormula(r, floatPnl, realizedPnl(r), totalPnl)}</span></div>
        </div>
      </td>
    `;
    tbody.appendChild(detail);
    bindRow(tr, 'click', () => {
      if (window.matchMedia('(max-width: 760px)').matches) {
        openBottomSheet(r.name || r.symbol || '持仓详情', detail.querySelector('td')?.innerHTML || detail.innerHTML);
        return;
      }
      detail.hidden = !detail.hidden;
      tr.classList.toggle('open', !detail.hidden);
    });
  }
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
          ${snapshotQuoteDate(r) ? `<span class="mobileHoldingSync">行情 ${esc(snapshotQuoteDate(r))}</span>` : ''}
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
    bindRow(node, 'click', () => {
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
          <div><strong>${dilutedCostLabel(row)}</strong><span class="mono">${esc(dilutedAveragePriceText(row))}</span></div>
          <div><strong>现价</strong><span class="mono">${esc(holdingPriceText(row))}</span></div>
          <div><strong>涨跌幅</strong><span class="mono ${dailyPending ? '' : Number(changePct) >= 0 ? 'up' : 'down'}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '--' : signedPercentText(changePct)}</span></div>
          <div><strong>数据</strong><span>${dataBadge(row)}</span></div>
          ${snapshotQuoteDate(row) ? `<div><strong>行情日期</strong><span>${esc(snapshotQuoteDate(row))}</span></div>` : ''}
          <div class="rowDetailFull"><strong>累计公式</strong><span class="mono">${cumulativeFormula(row, floatPnl, realized, totalPnl)}</span></div>
          ${accountBreakdown}
        </div>
      `;
      openBottomSheet(shortHoldingName(row), html);
    });
  });
}

return { dataBadge, holdingDailyStatusBadge, closedBadge, renderRows, holdingSyncTime, renderMobileHoldingRows, dispose: clearRows };
}
