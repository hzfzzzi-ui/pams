import {
  esc, cnyValue, safeNum, formatNumber,
  formatCompactCurrency, formatSignedCurrency, signedMoneyClass, dailyFxText,
  dailyNativeText, holdingDailyPnlPending, hasDailyTotalPnl, datedReturnRow,
  displayDailyPnl, cumulativePnl,
  holdingCostValue, realizedPnl, realizedPnlNote,
  dilutedAveragePriceText, holdingGroupLabel, holdingGroupRank, shortHoldingName,
  holdingPriceText, holdingQuantityText, holdingAveragePriceText, holdingPriceChangePct,
  signedPercentText, aggregateMobileHoldingRows
} from './list_legacy_model.js';
import { typeLabel, accountLabel } from '../../../api.js';
import { bindMarketDetail, marketDetailHtml } from './market_detail.js';
import { assetIconCaption, assetIconMarkup, installAssetIconFallback } from '../../../shared/asset_icons.js';

export function createListView({ document, host: window, showDetails: openBottomSheet, scope, openAccount }) {
  const rowListeners = new Set();
  const releaseAssetIcons = installAssetIconFallback(window.document);
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
  if (floatPnl == null || realized == null) return '部分盈亏分项待核对';
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

function holdingMetrics(row, summary) {
  const components = row.__accountRows || [row];
  const value = row.market_value_cny == null ? null : Number(row.market_value_cny);
  const shared = components.some(item => Number(item.owner_ratio) > 0 && Number(item.owner_ratio) < 1);
  const historical = Boolean(row.reference_as_of);
  return {
    value, shared,
    weight: row.portfolio_weight == null ? '—' : `${summary?.missing_values ? '≈' : ''}${(Number(row.portfolio_weight) * 100).toFixed(2)}%`,
    valueLabel: historical ? '参考市值' : shared ? '个人市值' : '市值',
    quantityLabel: shared ? '账户份数' : '份数',
    costValue: historical || row.cost_cny == null ? null : holdingCostValue(row),
    floatPnl: historical ? null : row.floating_pnl_cny ?? null,
    realized: historical || shared || row.realized_trade_pnl_cny == null ? null : realizedPnl(row),
    totalPnl: historical || shared || row.holding_pnl_cny == null ? null : cumulativePnl(row),
    dailyPnl: historical || (row.daily_pnl_cny == null && row.daily_total_pnl_cny == null) ? null : displayDailyPnl(row),
    dailyPending: !historical && holdingDailyPnlPending(row),
  };
}

function positionReturn(row) {
  const dividend = row.position_dividend_return;
  return dividend
    ? {amount:dividend.amount_cny, rate:dividend.yield_on_cost, label:'年内分红', rateLabel:'年内分红收益率（按持仓成本）'}
    : {amount:row.floating_pnl_cny, rate:row.floating_pnl_ratio, label:'浮动盈亏', rateLabel:'浮动盈亏率'};
}

function pnlText(value) {
  if (value == null) return '—';
  return Number(value) === 0 ? formatCompactCurrency(0) : formatSignedCurrency(value);
}

function pnlTone(value) {
  return value == null || Number(value) === 0 ? 'muted' : signedMoneyClass(value);
}

function holdingAccountName(row) {
  return row.account_name || accountLabel(row.account || row.account_code || '');
}

function openHoldingDetail(row, summary, fields) {
  const title = shortHoldingName(row);
  if (row.reference_as_of || holdingGroupLabel(row) === '现金/活期') {
    openBottomSheet(title, fields);
    return;
  }
  const metrics = holdingMetrics(row, summary);
  const accounts = [...new Set((row.__accountRows || [row]).map(holdingAccountName))];
  const body = openBottomSheet(title, marketDetailHtml({
    iconMarkup: assetIconMarkup(row, title),
    subtitle: [row.symbol, holdingGroupLabel(row), accounts.join(' / ')].filter(Boolean).join(' · '),
    value: metrics.value == null ? '—' : formatCompactCurrency(metrics.value), totalPnl: metrics.shared ? '待分段核对' : pnlText(metrics.totalPnl),
    pnlClass: pnlTone(metrics.totalPnl),
    breakdown: [
      {label:'剩余成本', amount:metrics.costValue == null ? '—' : formatCompactCurrency(metrics.costValue)},
      {label:'当前浮盈', amount:pnlText(metrics.floatPnl), tone:pnlTone(metrics.floatPnl)},
      {label:'浮动盈亏率', amount:row.floating_pnl_ratio == null ? '—' : signedPercentText(row.floating_pnl_ratio * 100), tone:pnlTone(row.floating_pnl_ratio)},
      {label:'市值占比', amount:metrics.weight},
      ...(metrics.shared && !row.__isAggregated ? [{label:'当前个人归属', amount:formatNumber(Number(row.owner_ratio) * 100) + '%'}] : []),
      ...(row.position_dividend_return ? [
        {label:'年内分红', amount:pnlText(row.position_dividend_return.amount_cny)},
        {label:'年内分红收益率（按持仓成本）', amount:row.position_dividend_return.yield_on_cost == null
          ? '—' : signedPercentText(Number(row.position_dividend_return.yield_on_cost) * 100)},
      ] : []),
      ...(metrics.realized == null ? [] : [{label:'已实现盈亏', amount:pnlText(metrics.realized), tone:pnlTone(metrics.realized)}]),
    ], fields,
  }));
  const sheet = body?.querySelector('.holdingMarketDetail');
  if (sheet) {
    const links = sheet.querySelector('.holdingAccountLinks');
    const members = row.__accountRows || [row];
    for (const code of new Set(members.map(item => item.account || item.account_code).filter(Boolean))) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = accountLabel(code) + ' · 账户收益 ›';
      button.onclick = () => openAccount(code);
      links.appendChild(button);
    }
  }
  bindMarketDetail(sheet, row, metrics.shared);
}

function renderFinancing(items) {
  clearRows();
  document.getElementById('holdings-smart-table').classList.add('showFinancing');
  const list = document.getElementById('financingList');
  list.hidden = false;
  if (!items?.length) {
    list.innerHTML = `<div class="mobileEmpty">${items ? '暂无投资融资负债' : '融资明细待更新'}</div>`;
    return;
  }
  const fields = items.map(item => {
    const kind = item.kind === 'negative_cash' ? (item.amount_cny == null ? '现金余额待核验' : '账户负现金') : '投资借款';
    return {
      title: item.name || `${item.account_name} · ${kind}`,
      amount: item.amount_cny == null ? '待核验' : formatCompactCurrency(item.amount_cny),
      share: item.share == null ? '—' : `${(Number(item.share) * 100).toFixed(2)}%`,
      detail: [['所属账户', item.account_name], ['融资类型', kind], ['资料日期', item.as_of || '—']],
    };
  });
  list.innerHTML = '<div class="personalHoldingHeading"><span>融资明细</span><span>占融资负债</span></div>'
    + fields.map((row, i) => `<button class="mobileHoldingRow personalPosition" type="button" data-financing-index="${i}">
      <span class="personalPositionTitle"><strong>${esc(row.title)}</strong><b>${row.share}</b></span>
      <span>负债余额　${row.amount}</span></button>`).join('');
  list.querySelectorAll('[data-financing-index]').forEach(node => bindRow(node, 'click', () => {
    const row = fields[Number(node.dataset.financingIndex)];
    const details = [...row.detail, ['负债余额', row.amount], ['占融资负债', row.share]];
    openBottomSheet(row.title, '<div class="rowDetailGrid">' + details.map(([label, value]) =>
      `<div><strong>${esc(label)}</strong><span>${esc(value)}</span></div>`).join('') + '</div>');
  }));
}

function renderRows(rows, options = {}){
  document.getElementById('holdings-smart-table').classList.remove('showFinancing');
  document.getElementById('financingList').hidden = true;
  clearRows();
  const tbody = document.getElementById('rows');
  tbody.innerHTML='';
  const summary = options.summary;
  const showGroupRows = Boolean(options.showGroupRows);
  const displayRows = [...rows].sort((a, b) => {
    const groupA = holdingGroupLabel(a);
    const groupB = holdingGroupLabel(b);
    return holdingGroupRank(groupA) - holdingGroupRank(groupB)
      || groupA.localeCompare(groupB, 'zh-CN')
      || safeNum(cnyValue(b, 'market_value')) - safeNum(cnyValue(a, 'market_value'));
  });
  renderMobileHoldingRows(displayRows, { summary });
  let lastGroup = '';
  for(const r of displayRows){
    const { value, weight, valueLabel, costValue, floatPnl, realized, totalPnl, dailyPnl, dailyPending } = holdingMetrics(r, summary);
    const listed = positionReturn(r);
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
      <td class="mono num">${value == null ? '—' : formatCompactCurrency(value)}</td>
      <td class="mono num">${holdingQuantityText(r)}</td>
      <td class="mono num">${r.current_price ? formatNumber(r.current_price, 3) : '价格缺失'}</td>
      <td class="mono num ${dailyPending ? 'muted' : pnlTone(dailyPnl)}">${dailyPending ? '待更新' : pnlText(dailyPnl)}</td>
      <td title="${listed.label}" class="mono num ${pnlTone(listed.amount)}">${pnlText(listed.amount)}</td>
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
          <div><strong>${valueLabel}</strong><span class="mono">${value == null ? '—' : formatCompactCurrency(value)}</span></div>
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
    bindRow(tr, 'click', () => openHoldingDetail(r, summary, fields));
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

function renderMobileHoldingRows(rows, { summary }) {
  const el = document.getElementById('mobileHoldingList');
  if (!el) return;

  const displayRows = aggregateMobileHoldingRows(rows);
  if (!rows.length) {
    el.innerHTML = '<div class="mobileEmpty">暂无持仓</div>';
    return;
  }
  el.innerHTML = displayRows.map((raw, index) => {
    const r = datedReturnRow(raw);
    const {weight, value, dailyPnl, dailyPending} = holdingMetrics(r, summary);
    const {amount, rate, label, rateLabel} = positionReturn(r);
    const daily = dailyPending ? null : dailyPnl;
    const dailyRate = dailyPending || r.reference_as_of ? null : holdingPriceChangePct(r);
    const name = shortHoldingName(r);
    const status = r.reference_as_of ? '历史参考' : r.is_closed ? '已平仓' : '';
    return `<button class="mobileHoldingRow personalPosition" type="button" data-mobile-holding-index="${index}">
      <span class="personalPositionIdentity">
        ${assetIconMarkup(r, name)}
        <span class="personalPositionName"><strong>${esc(name)}</strong><small>${esc([status || holdingGroupLabel(r), assetIconCaption(r)].filter(Boolean).join(' · '))}</small></span>
      </span>
      <span class="personalPositionValues">
        <b title="个人持仓市值"><span class="metricCaption">个人持仓市值</span>${value == null ? '—' : formatCompactCurrency(value)}</b>
        <small class="personalPositionWeight" title="仓位占比"><span class="metricCaption">仓位占比</span>${weight}</small>
        <small class="${pnlTone(amount)}" title="${label}"><span class="metricCaption">${label}</span>${pnlText(amount)}</small>
        <small class="${pnlTone(rate)}" title="${rateLabel}"><span class="metricCaption">${rateLabel}</span>${rate == null ? '—' : (Number(rate) === 0 ? '0.00%' : signedPercentText(rate * 100))}</small>
        <small class="${pnlTone(daily)}" title="最近一日盈亏"><span class="metricCaption">最近一日盈亏</span>${pnlText(daily)}</small>
        <small class="${pnlTone(dailyRate)}" title="最近一日涨跌幅"><span class="metricCaption">最近一日涨跌幅</span>${signedPercentText(dailyRate)}</small>
      </span>
    </button>`;
  }).join('');
  el.querySelectorAll('[data-mobile-holding-index]').forEach((node) => {
    bindRow(node, 'click', () => openPosition(displayRows[Number(node.dataset.mobileHoldingIndex)], summary));
  });
}

function openPosition(raw, summary) {
  const row = datedReturnRow(raw);
  const { value, weight, valueLabel, costValue, floatPnl, realized, totalPnl, dailyPnl, dailyPending } = holdingMetrics(row, summary);
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
      <div><strong>${valueLabel}</strong><span class="mono">${value == null ? '—' : formatCompactCurrency(value)}</span></div>
      <div><strong>剩余成本</strong><span class="mono">${costValue == null ? '—' : formatCompactCurrency(costValue)}</span></div>
      <div><strong>当前浮盈</strong><span class="mono ${pnlTone(floatPnl)}">${pnlText(floatPnl)}</span></div>
      <div><strong>已实现</strong><span class="mono ${pnlTone(realized)}">${pnlText(realized)}</span></div>
      <div><strong>累计盈亏</strong><span class="mono ${pnlTone(totalPnl)}">${pnlText(totalPnl)}</span></div>
      <div><strong>${esc(row.__dailyReturnLabel || '今日盈亏')}</strong><span class="mono ${dailyPending ? 'muted' : pnlTone(dailyPnl)}">${dailyPending ? '待更新' : pnlText(dailyPnl)}</span></div>
      <div><strong>收益状态</strong><span>${dailyPending ? '待更新' : holdingDailyStatusBadge(row) || '<span class="muted">—</span>'}</span></div>
      ${hasDailyTotalPnl(row) ? `<div><strong>价格影响</strong><span class="mono ${dailyPending ? '' : signedMoneyClass(priceDailyPnl)}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '--' : formatSignedCurrency(priceDailyPnl)}</span></div>` : ''}
      ${detailNativeDaily ? `<div><strong>${row.__dailyReturnDate ? `本币${esc(row.__dailyReturnDate.slice(5).replace('-', '/'))}` : '本币今日'}</strong><span class="mono">${esc(detailNativeDaily)}</span></div>` : ''}
      ${detailFxDaily ? `<div><strong>证券汇兑估算</strong><span class="mono ${dailyPending ? '' : signedMoneyClass(cnyValue(row, 'daily_fx_pnl'))}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${esc(detailFxDaily.replace(/^证券汇兑\s*/, ''))}</span></div>` : ''}
      <div><strong>估值时间</strong>${holdingSyncTime(row)}</div>
      <div><strong>${holdingMetrics(row, summary).quantityLabel}</strong><span class="mono">${esc(holdingQuantityText(row))}</span></div>
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
  openHoldingDetail(row, summary, html);
}

return { openPosition, dataBadge, holdingDailyStatusBadge, closedBadge, renderRows, renderFinancing, holdingSyncTime, renderMobileHoldingRows, dispose() { clearRows(); releaseAssetIcons(); } };
}
