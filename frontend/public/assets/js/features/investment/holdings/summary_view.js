import {
  cnyValue, fmtUsd, fmtCny, fmtSignedUsd,
  fmtSignedCny, fmtPctValue, formatCurrency, formatCompactCurrency,
  formatTableNumber, formatPercentRate, profitColor, toneFromNumber,
  isInternalInvestmentLabel, pctFromAmount, signedPctText, formatSyncTimestamp,
  normalizeMobileHeroGroup, shareTone, formatOptionalMoney, isSameOrAfterDate,
  contributionPct, formatNativeDaily, esc, displayPnlPct,
  fmtSignedPctFromRate, sectionTodayReturnAmount, annualizedRateFromPeriod, buildInvestmentConclusion
} from './summary_legacy_model.js?v=20261031-modules-v1';
import { fmt2, accountLabel } from '../../../api.js?v=20261031-modules-v1';
import { safeNum, pnlPct } from '../../../utils.js?v=20261031-modules-v1';
import { CHART_THEME, SERIES_COLORS } from '../../../shared/chart_theme.js?v=20261031-modules-v1';
import {
  RETURN_METHODS
} from './constants.js?v=20261031-modules-v1';

export function createSummaryView({ document, host: window, showDetails: openBottomSheet, scope }) {
  let mobileHeroContext = null;
  let publishedSummary = null;
function statusBadge(label, kind = 'data') {
  return `<span class="statusBadge statusBadge-${kind}">${esc(label)}</span>`;
}

function ReturnMethodBadge(method) {
  return `
    <div class="returnMethodBadge" title="${esc(method.text)}">
      <span>${esc(method.label)}</span>
    </div>
  `;
}

function metricCard(label, value, help, kind = '', extra = '') {
  const extraClass = extra ? ' hasBreakdown' : '';
  return `
    <div class="investmentHeroMetric ${kind}${extraClass}">
      <div class="label">${esc(label)}</div>
      <div class="value">${value}</div>
      <div class="help">${esc(help)}</div>
      ${extra}
    </div>
  `;
}

function InvestmentSummaryHeader(summary) {
  return `
    ${metricCard('市值', formatCompactCurrency(summary.marketValue), '含持仓与现金/活期', 'info')}
    ${metricCard('累计收益', formatCompactCurrency(summary.totalPnl, 'CNY', { signed: true }), '综合口径', summary.totalPnl >= 0 ? 'good' : 'bad')}
    ${metricCard('收益率', formatPercentRate(summary.twr), '总盈亏 / 成本', summary.twr >= 0 ? 'good' : 'bad')}
    ${metricCard('今日', formatCompactCurrency(summary.dailyPnl, 'CNY', { signed: true }), '日度变动', summary.dailyPnl >= 0 ? 'good' : 'bad')}
  `;
}

function InvestmentSummaryHeaderCompact(summary) {
  const hasReturnRate = Number.isFinite(Number(summary.twr));
  const returnRate = hasReturnRate ? Number(summary.twr) : null;
  const dailyPnl = Number.isFinite(Number(summary.dailyPnl)) ? Number(summary.dailyPnl) : null;
  const currentFloatPnl = Number.isFinite(Number(summary.currentFloatPnl)) ? Number(summary.currentFloatPnl) : null;
  return `
    ${metricCard('投资组合价值', formatCompactCurrency(summary.marketValue), '含持仓与现金/活期', 'info')}
    ${metricCard('收益率', hasReturnRate ? formatPercentRate(returnRate) : '待核准', '总盈亏 / 成本', toneFromNumber(returnRate))}
    ${metricCard('今日收益', dailyPnl == null ? '待核准' : formatCompactCurrency(dailyPnl, 'CNY', { signed: true }), '今日投资收益变化', toneFromNumber(dailyPnl), DailyReturnBreakdown(summary.dailyBreakdown))}
    ${metricCard('总收益', formatCompactCurrency(summary.totalPnl, 'CNY', { signed: true }), '累计投资收益', summary.totalPnl >= 0 ? 'good' : 'bad')}
    ${metricCard('当前浮盈', currentFloatPnl == null ? '待核准' : formatCompactCurrency(currentFloatPnl, 'CNY', { signed: true }), '仍持仓标的浮动盈亏', toneFromNumber(currentFloatPnl))}
  `;
}

function DailyReturnBreakdown(items = []) {
  const rows = (items || []).filter(item => !isInternalInvestmentLabel(item.label)).map(item => {
    const price = Number.isFinite(Number(item.priceDailyPnl)) ? safeNum(item.priceDailyPnl) : safeNum(item.dailyPnl);
    const fx = safeNum(item.fxDailyPnl);
    const value = Number.isFinite(Number(item.dailyTotalPnl)) ? safeNum(item.dailyTotalPnl) : price + fx;
    return `
      <span class="dailyReturnChip ${value >= 0 ? 'good' : 'bad'}" title="${esc(item.label)} 今日收益">
        <span>${esc(item.label)}</span>
        <strong>${formatCompactCurrency(value, 'CNY', { signed: true })}</strong>
        ${Math.abs(fx) > 0.005 ? `<em>价格 ${formatCompactCurrency(price, 'CNY', { signed: true })} · 证券汇兑估算 ${formatCompactCurrency(fx, 'CNY', { signed: true })}</em>` : ''}
      </span>
    `;
  }).join('');
  return rows ? `<div class="dailyReturnBreakdown">${rows}</div>` : '';
}

function currentMobileHeroGroup() {
  return document.getElementById('holdingGroupFilter')?.value || '全部';
}

function mobileHeroMetaItem(label, value, tone = '') {
  if (value == null || value === '') return '';
  return `
    <div class="mobileHeroMiniMetric ${tone}">
      <span>${esc(label)}</span>
      <strong>${value}</strong>
    </div>
  `;
}

function MobilePortfolioHero(profile, asOf, dataIssues = [], syncAt = null) {
  const value = safeNum(profile.value);
  const dailyPending = profile.dailyStatus === 'pending';
  const dailyPartial = profile.dailyStatus === 'partial';
  const dailyHistorical = profile.dailyStatus === 'historical';
  const dailyPnl = Number.isFinite(Number(profile.dailyPnl)) ? safeNum(profile.dailyPnl) : 0;
  const dailyTone = dailyPending ? '' : dailyPnl >= 0 ? 'up' : 'down';
  const dailyStatusLabel = { pending: '待更新', partial: '仅含已更新标的，仍有持仓待更新', estimated: '预估收益', confirmed: '今日收益已确认', historical: profile.dailyStatusTitle }[profile.dailyStatus] || '';
  const dailyBase = Math.max(value - dailyPnl, 1);
  const dailyRate = Number.isFinite(Number(profile.dailyRate)) ? safeNum(profile.dailyRate) : pctFromAmount(dailyPnl, dailyBase);
  const totalPnl = Number.isFinite(Number(profile.totalPnl)) ? safeNum(profile.totalPnl) : 0;
  const totalRate = Number.isFinite(Number(profile.totalRate)) ? safeNum(profile.totalRate) : 0;
  const valueDisplay = formatOptionalMoney(value, 'CNY', { signed: Boolean(profile.valueSigned) });
  const rawShare = safeNum(profile.share);
  const shareWidth = Math.abs(rawShare);
  const tone = shareTone(rawShare);
  const miniMetrics = (profile.miniMetrics || []).map(item => mobileHeroMetaItem(item.label, item.value, item.tone)).join('');
  return `
    ${profile.hideHeader ? '' : `<div class="mobileAccountHeroTop">
      <div class="mobileAccountIdentity">
        <span class="mobileAccountIcon">${profile.icon || '◌'}</span>
        <span>${esc(profile.identity || 'CNY PAMS')}</span>
      </div>
      <div class="mobileAccountWeight ${tone}">
        <span>仓位 ${signedPctText(rawShare).replace('+', '')}</span>
        <i><b style="width:${Math.max(2, Math.min(100, shareWidth * 100))}%"></b></i>
      </div>
    </div>`}
    <div class="mobileHeroPrimaryGrid">
      <div class="mobileHeroPrimaryMetric main">
        <span>${esc(profile.valueLabel || '净清算价值')}</span>
        <strong class="mobileHeroValue ${value < 0 ? 'down' : ''}">${valueDisplay}</strong>
      </div>
      <div class="mobileHeroPrimaryMetric">
        <span${dailyStatusLabel ? ` title="${esc(dailyStatusLabel)}"` : ''}>${esc(dailyPartial ? '今日盈亏（部分）' : profile.dailyLabel || '今日盈亏')}</span>
        <strong class="${dailyTone}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending ? '待更新' : formatOptionalMoney(dailyPnl, 'CNY', { signed: true })}</strong>
        <em class="${dailyTone}"${dailyPending ? ' style="color:var(--muted)"' : ''}>${dailyPending || dailyPartial || dailyHistorical ? '--' : signedPctText(dailyRate)}</em>
      </div>
      <div class="mobileHeroPrimaryMetric">
        <span>${esc(profile.totalLabel || '总盈亏')}</span>
        <strong class="${totalPnl >= 0 ? 'up' : 'down'}">${formatOptionalMoney(totalPnl, 'CNY', { signed: true })}</strong>
        <em class="${totalPnl >= 0 ? 'up' : 'down'}"${profile.totalRateTitle ? ` title="${esc(profile.totalRateTitle)}"` : ''}>${profile.totalRateUndefined === true ? '--' : signedPctText(totalRate)}</em>
      </div>
    </div>
    <div class="mobileHeroMiniGrid ${miniMetrics ? '' : 'mobileHeroMiniGrid-empty'}">${miniMetrics}</div>
  `;
}

function updateMobilePortfolioHero(group = currentMobileHeroGroup()) {
  if (!mobileHeroContext) return;
  const mobileHeroEl = document.getElementById('mobilePortfolioHero');
  if (!mobileHeroEl) return;
  const selected = normalizeMobileHeroGroup(group);
  const profile = mobileHeroContext.profiles[selected] || mobileHeroContext.profiles['全部'];
  mobileHeroEl.classList.toggle('mobileHeroAll', Boolean(profile.hideHeader));
  mobileHeroEl.classList.toggle('mobileHeroCategory', !profile.hideHeader);
  mobileHeroEl.innerHTML = MobilePortfolioHero(profile, mobileHeroContext.asOf, mobileHeroContext.dataIssues, mobileHeroContext.syncAt);
}

function bindMobileMethodology(summary, dataIssues, asOf) {
  const btn = document.getElementById('mobileMethodologyTrigger');
  if (!btn) return;
  scope.listen(btn, 'click', () => {
    openBottomSheet('投资口径', `
      <div class="rowDetailGrid">
        <div><strong>快照日期</strong><span>${esc(asOf || '待核准')}</span></div>
        <div><strong>组合价值</strong><span class="mono">${formatCompactCurrency(summary.marketValue)}</span></div>
        <div><strong>总负债</strong><span class="mono">${formatCompactCurrency(safeNum(summary.totalLiabilities))}</span></div>
        <div><strong>现金/活期</strong><span class="mono">${formatCompactCurrency(summary.cashActiveValue, 'CNY', { signed: safeNum(summary.cashActiveValue) < 0 })}</span></div>
        <div><strong>现金负债</strong><span class="mono">${formatCompactCurrency(-safeNum(summary.cashActiveLiabilities), 'CNY', { signed: safeNum(summary.cashActiveLiabilities) > 0 })}</span></div>
        <div><strong>今日收益</strong><span class="mono">${formatCompactCurrency(summary.dailyPnl, 'CNY', { signed: true })}</span></div>
        <div><strong>总收益率</strong><span class="mono">${formatPercentRate(summary.twr)}</span></div>
      </div>
      ${dataIssues.length ? `<div class="issueList" style="margin-top:12px">${dataIssues.map(issue => `<div class="issueRow"><span>${esc(issue)}</span></div>`).join('')}</div>` : ''}
    `);
  });
}

function DataQualityBanner(issues) {
  if (!issues.length) return '';
  return `
    <a class="investmentDataDetails investmentDataLink" href="data_sources.html#dataQuality">口径 ${issues.length} 项</a>
  `;
}

function contributionItem(item, totalPnl, mode = 'total') {
  const value = mode === 'daily' ? item.totalDailyPnl : item.pnl;
  const rate = item.returnPct;
  const pct = contributionPct(value, totalPnl);
  const dailyMeta = mode === 'daily'
    ? [
        `${formatNativeDaily(item.nativeDailyPnl, item.currency)}`,
        Math.abs(safeNum(item.fxDailyPnl)) > 0.005 ? `汇率 ${fmtSignedCny(item.fxDailyPnl)}` : '',
      ].filter(Boolean).join(' · ')
    : formatPercentRate(rate);
  return `
    <div class="contributionItem" title="${esc([item.assetClass, item.account].filter(Boolean).join(' / ') || '')}">
      <div class="contributionMain">
        <strong>${esc(item.name)}</strong>
        <span>${statusBadge(item.assetClass, item.quality.kind)}</span>
      </div>
      <div class="contributionNumbers">
        <span style="color:${profitColor(value)}">${formatCompactCurrency(value, 'CNY', { signed: true })}</span>
        <small>${esc(dailyMeta)}</small>
      </div>
    </div>
  `;
}

function ContributionList(title, items, totalPnl, options = {}) {
  const mode = options.mode || 'total';
  const body = items.length
    ? items.slice(0, 5).map(item => contributionItem(item, totalPnl, mode)).join('')
    : `<div class="emptyState">${esc(options.emptyText || '现有接口没有足够数据，待补充。')}</div>`;
  return `
    <div class="contributionCard">
      ${title ? `<div class="sectionTitle">
        <h3>${esc(title)}</h3>
        ${options.badge ? statusBadge(options.badge, options.badgeKind || 'data') : ''}
      </div>` : ''}
      ${body}
    </div>
  `;
}

function ProfitContributionPanel(model) {
  const groups = [
    { key: 'win', label: '赚钱', html: ContributionList('', model.winners, model.totalPnl) },
    { key: 'loss', label: '亏损', html: ContributionList('', model.losers, model.totalPnl) },
    { key: 'day', label: '今日', html: ContributionList('', model.daily, model.dailyPnl, {
      mode: 'daily',
      emptyText: '单资产日度待补充。',
    }) },
  ];
  return `<div class="contributionTabs">
    <div class="segmented" data-contribution-tabs>
      ${groups.map((g, i) => `<button type="button" class="${i === 0 ? 'active' : ''}" data-contribution-tab="${esc(g.key)}">${esc(g.label)}</button>`).join('')}
    </div>
    ${groups.map((g, i) => `<div class="contributionPane" data-contribution-pane="${esc(g.key)}" ${i === 0 ? '' : 'hidden'}>${g.html}</div>`).join('')}
  </div>`;
}

function topMoverRow(item, options = {}) {
  const mode = options.mode || 'total';
  const value = mode === 'daily' ? safeNum(item.totalDailyPnl) : safeNum(item.pnl);
  const totalPnl = safeNum(options.totalPnl);
  const name = item.name || item.label || '未命名';
  const detail = [item.assetClass, item.account].filter(Boolean).join(' / ');
  const meta = mode === 'period'
    ? [
        formatPercentRate(item.returnPct),
        `贡献 ${formatPercentRate(contributionPct(value, totalPnl))}`,
        `期末占比 ${(safeNum(item.weight) * 100).toFixed(1)}%`,
      ].join(' · ')
    : mode === 'daily'
    ? [formatNativeDaily(item.nativeDailyPnl, item.currency), Math.abs(safeNum(item.fxDailyPnl)) > 0.005 ? `汇率 ${fmtSignedCny(item.fxDailyPnl)}` : '']
      .filter(Boolean).join(' · ')
    : `${formatPercentRate(item.returnPct)} · 权重 ${(safeNum(item.weight) * 100).toFixed(1)}%`;
  return `
    <div class="topMoverRow ${value >= 0 ? 'good' : 'bad'}">
      <div class="topMoverName">
        <strong>${esc(name)}</strong>
        <span>${esc(detail)}</span>
      </div>
      <div class="topMoverValue">
        <strong>${formatCompactCurrency(value, 'CNY', { signed: true })}</strong>
        <span>${esc(meta)}</span>
      </div>
    </div>
  `;
}

function topMoverGroup(title, rows, emptyText, options = {}) {
  return `
    <div class="topMoverGroup">
      <div class="topMoverGroupTitle">${esc(title)}</div>
      ${rows.length
        ? rows.slice(0, 3).map(item => topMoverRow(item, options)).join('')
        : `<div class="returnInsightEmpty">${esc(emptyText)}</div>`}
    </div>
  `;
}

function TopContributionPanel(model) {
  const winners = model?.winners || [];
  const losers = model?.losers || [];
  const daily = model?.daily || [];
  const unattributed = model?.unattributed || null;
  const mode = model?.mode || 'total';
  const windowLabel = model?.windowLabel || '当前';
  const totalPnl = safeNum(model?.totalPnl);
  const titleHint = mode === 'period'
    ? `${windowLabel}区间收益变化，随上方日期段切换`
    : '当前持仓收益贡献，不含自动调仓建议';
  const badge = mode === 'period' ? statusBadge('区间口径', 'data') : statusBadge('持仓快照', 'data');
  if (mode === 'period') {
    return `
      <div class="returnInsightHead">
        <div><h3>Top 贡献</h3><span>${esc(titleHint)}</span></div>
        ${badge}
      </div>
      <div class="topMoverTotal ${totalPnl >= 0 ? 'good' : 'bad'}">
        <span>${esc(windowLabel)}合计</span>
        <strong>${formatCompactCurrency(totalPnl, 'CNY', { signed: true })}</strong>
      </div>
      <div class="topMoverGrid">
        ${topMoverGroup('贡献最大', winners, '当前区间没有正贡献资产桶', { mode: 'period', totalPnl })}
        ${topMoverGroup('拖累最大', losers, '当前区间没有明显拖累资产桶', { mode: 'period', totalPnl })}
      </div>
      ${unattributed ? `<div class="returnInsightEmpty">
        未归因差额 ${formatCompactCurrency(unattributed.pnl, 'CNY', { signed: true })} · ${esc(unattributed.note)}
      </div>` : ''}
    `;
  }
  return `
    <div class="returnInsightHead">
      <div><h3>Top 贡献</h3><span>${esc(titleHint)}</span></div>
      ${badge}
    </div>
    <div class="topMoverGrid">
      ${topMoverGroup('贡献最大', winners, '暂无正贡献标的')}
      ${topMoverGroup('拖累最大', losers, '暂无明显拖累标的')}
    </div>
    ${daily.length ? `<div class="topMoverToday">
      <div class="topMoverGroupTitle">今日波动</div>
      ${daily.slice(0, 2).map(item => topMoverRow(item, { mode: 'daily' })).join('')}
    </div>` : ''}
  `;
}

function setKpiLabel(valueId, label) {
  const valueEl = document.getElementById(valueId);
  const labelEl = valueEl?.closest('.kpi')?.querySelector('.kpi-label');
  if (labelEl) labelEl.textContent = label;
}

function renderIbkrPerformance(perf, positionAsOf = null) {
  if (!perf || perf.status !== 'ok') return null;

  const flexCoversPositionDate = !positionAsOf || isSameOrAfterDate(perf.as_of, positionAsOf);
  const accountNetDeposit = perf.net_deposit_cny == null || perf.net_deposit_cny === ''
    ? null
    : safeNum(perf.net_deposit_cny);
  const accountTotalPnl = perf.total_pnl_cny == null || perf.total_pnl_cny === ''
    ? null
    : safeNum(perf.total_pnl_cny);
  const summary = {
    totalValue: safeNum(perf.net_liquidation_cny),
    totalCost: accountNetDeposit,
    totalPnl: accountTotalPnl,
    dailyPnl: null,
    positionValue: safeNum(perf.position_value_cny),
    cashValue: safeNum(perf.cash_cny),
    totalReturnRate: safeNum(perf.return_ratio),
    asOf: perf.as_of,
    flexCoversPositionDate,
  };

  if (!flexCoversPositionDate) return summary;

  setKpiLabel('totalValueUSStock', '账户净值');
  setKpiLabel('totalPnlUSStock', '账户收益');
  setKpiLabel('countKpiUSStock', '杠杆比例');
  setKpiLabel('holdingPnlUSStock', '账户资金收益率');

  const totalPnl = safeNum(perf.total_pnl_base);
  const totalPnlEl = document.getElementById('totalPnlUSStock');

  document.getElementById('totalValueUSStock').textContent = fmtUsd(perf.net_liquidation_base);
  totalPnlEl.textContent = fmtSignedUsd(totalPnl);
  totalPnlEl.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  document.getElementById('countKpiUSStock').textContent = fmtPctValue(perf.leverage_ratio);
  const rateEl = document.getElementById('holdingPnlUSStock');
  if (rateEl) {
    rateEl.textContent = fmtSignedPctFromRate(perf.return_ratio);
    rateEl.style.color = safeNum(perf.return_ratio) >= 0 ? 'var(--good)' : 'var(--bad)';
  }

  return summary;
}

function renderAStockOwnerPerformance(perf) {
  if (!perf || perf.status !== 'ok') return null;

  setKpiLabel('totalValueAStockEquity', '个人净值');
  setKpiLabel('totalPnlAStockEquity', '总盈亏');
  setKpiLabel('countKpiAStockEquity', '持股比例');
  setKpiLabel('holdingPnlAStockEquity', '持仓收益率');

  const accountTotalPnl = safeNum(perf.total_pnl_cny);
  const dailyPnlEffective = perf.daily_pnl_effective !== false;
  const dailyPnl = dailyPnlEffective && Number.isFinite(Number(perf.daily_pnl_cny))
    ? safeNum(perf.daily_pnl_cny)
    : null;

  const dailyPnlEl = document.getElementById('dailyPnlAStockEquity');

  document.getElementById('totalValueAStockEquity').textContent = fmtCny(perf.net_liquidation_cny);
  if (dailyPnlEl && dailyPnl !== null) {
    dailyPnlEl.textContent = fmtSignedCny(dailyPnl);
    dailyPnlEl.style.color = dailyPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  document.getElementById('countKpiAStockEquity').textContent = fmtPctValue(perf.owner_ratio);

  return {
    totalValue: safeNum(perf.net_liquidation_cny),
    accountTotalPnl,
    dailyPnl,
    dailyPnlEffective,
    accountReturnRate: safeNum(perf.return_ratio),
  };
}

function renderRowStock(r){
  const pct = displayPnlPct(r);
  const symbol = r.symbol || '—';
  const name = r.name || '';
  const quantityText = r.quantity ? formatTableNumber(r.quantity, 3) : '数量缺失';
  return `
    <td class="nowrap" title="${esc(symbol)}">${esc(symbol)}</td>
    <td class="name" title="${esc(name)}"><span class="mobileHoldingQtyPill">${esc(quantityText)}</span><span class="holdingNameText">${esc(name)}</span>${closedBadge(r)}</td>
    <td class="mono num">${r.buy_price ? formatTableNumber(r.buy_price, 3) : '价格缺失'}</td>
    <td class="mono num">${quantityText}</td>
    <td class="mono num">${r.current_price ? formatTableNumber(r.current_price, 3) : '价格缺失'}</td>
    <td class="mono num">${formatCurrency(cnyValue(r, 'market_value'), 'CNY', { maximumFractionDigits: 2 })}</td>
    <td class="mono num" style="color:${pct>=0?'var(--good)':'var(--bad)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</td>
  `;
}

function renderRowFundLike(r){
  const pct = displayPnlPct(r);
  const name = r.name || '';
  const quantityText = r.quantity ? formatTableNumber(r.quantity, 3) : '份额缺失';
  return `
    <td class="name" title="${esc(name)}"><span class="mobileHoldingQtyPill">${esc(quantityText)}</span><span class="holdingNameText">${esc(name)}</span>${closedBadge(r)}</td>
    <td class="mono num">${r.buy_price ? formatTableNumber(r.buy_price, 3) : '净值型/待核准'}</td>
    <td class="mono num">${quantityText}</td>
    <td class="mono num">${r.current_price ? formatTableNumber(r.current_price, 3) : '净值型/待核准'}</td>
    <td class="mono num">${formatCurrency(cnyValue(r, 'market_value'), 'CNY', { maximumFractionDigits: 2 })}</td>
    <td class="mono num" style="color:${pct>=0?'var(--good)':'var(--bad)'}">${pct>=0?'+':''}${pct.toFixed(2)}%</td>
  `;
}

function closedBadge(r) {
  return r.is_closed ? '<span class="closedBadge">已平仓</span>' : '';
}

function renderSection(rows, renderRowFn, tbodyId, countId, totalValueId, totalPnlId, dailyPnlId, countKpiId, holdingPnlId, fxRates){
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML='';

  let totalValue = 0;
  let totalPnl = 0;
  let dailyPnl = 0;
  let fxDailyPnl = 0;
  let dailyTotalPnl = 0;
  let totalCost = 0;
  let activeHoldingPnl = 0;
  let activeTotalCost = 0;

  for(const r of rows){
    const tr=document.createElement('tr');
    tr.innerHTML = renderRowFn(r);
    tbody.appendChild(tr);

    totalValue += safeNum(cnyValue(r, 'market_value'));
    totalPnl += safeNum(cnyValue(r, 'holding_pnl'));
    const rowDailyPnl = safeNum(cnyValue(r, 'daily_pnl'));
    const rowFxDailyPnl = safeNum(cnyValue(r, 'daily_fx_pnl'));
    const rowDailyTotalPnl = (r.daily_total_pnl_cny != null || r.daily_total_pnl != null)
      ? safeNum(cnyValue(r, 'daily_total_pnl'))
      : rowDailyPnl + rowFxDailyPnl;
    dailyPnl += rowDailyPnl;
    fxDailyPnl += rowFxDailyPnl;
    dailyTotalPnl += rowDailyTotalPnl;
    const rowCost = safeNum(cnyValue(r, 'cost'));
    totalCost += rowCost;
    if (!r.is_closed) {
      activeHoldingPnl += safeNum(cnyValue(r, 'holding_pnl'));
      activeTotalCost += rowCost;
    }
  }

  document.getElementById(countId).textContent = rows.length;
  document.getElementById(countKpiId).textContent = rows.length;
  document.getElementById(totalValueId).textContent = '¥' + fmt2(totalValue);

  const pnlElem = document.getElementById(totalPnlId);
  pnlElem.textContent = (totalPnl >= 0 ? '+¥' : '-¥') + fmt2(Math.abs(totalPnl));
  pnlElem.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  const dailyElem = document.getElementById(dailyPnlId);
  dailyElem.textContent = fmtSignedCny(dailyTotalPnl);
  dailyElem.style.color = dailyTotalPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  setKpiFxNote(dailyPnlId, fxDailyPnl);

  if (holdingPnlId) {
    setKpiLabel(holdingPnlId, '持仓收益率');
    const hpElem = document.getElementById(holdingPnlId);
    const sectionPct = activeTotalCost > 0 ? (activeHoldingPnl / activeTotalCost * 100) : 0;
    hpElem.textContent = (sectionPct >= 0 ? '+' : '') + sectionPct.toFixed(2) + '%';
    hpElem.style.color = sectionPct >= 0 ? 'var(--good)' : 'var(--bad)';
  }

  return {
    totalValue,
    totalPnl,
    dailyPnl,
    fxDailyPnl,
    dailyTotalPnl,
    totalCost,
    activeHoldingPnl,
    activeTotalCost,
    holdingReturnRate: activeTotalCost > 0 ? activeHoldingPnl / activeTotalCost : 0,
  };
}

function writeSectionFinancials(summary, totalValueId, totalPnlId, dailyPnlId, holdingPnlId) {
  const { totalValue, totalPnl, dailyPnl, totalCost } = summary;
  document.getElementById(totalValueId).textContent = fmtCny(totalValue);

  const pnlElem = document.getElementById(totalPnlId);
  pnlElem.textContent = fmtSignedCny(totalPnl);
  pnlElem.style.color = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  const dailyElem = document.getElementById(dailyPnlId);
  dailyElem.textContent = fmtSignedCny(dailyPnl);
  dailyElem.style.color = dailyPnl >= 0 ? 'var(--good)' : 'var(--bad)';

  if (holdingPnlId) {
    setKpiLabel(holdingPnlId, summary.returnRateLabel || '持仓收益率');
    const hpElem = document.getElementById(holdingPnlId);
    if (summary.accountReturnRateUndefined === true) {
      hpElem.textContent = '--';
      hpElem.style.color = 'var(--muted)';
      return;
    }
    const explicitRate = Number(summary.displayReturnRate ?? summary.holdingReturnRate ?? summary.totalReturnRate);
    const sectionCost = safeNum(summary.activeTotalCost) > 0 ? safeNum(summary.activeTotalCost) : totalCost;
    const sectionPnl = summary.activeHoldingPnl != null ? safeNum(summary.activeHoldingPnl) : totalPnl;
    const sectionPct = Number.isFinite(explicitRate)
      ? explicitRate * 100
      : (sectionCost > 0 ? (sectionPnl / sectionCost * 100) : 0);
    hpElem.textContent = (sectionPct >= 0 ? '+' : '') + sectionPct.toFixed(2) + '%';
    hpElem.style.color = sectionPct >= 0 ? 'var(--good)' : 'var(--bad)';
  }
}

function renderReturnNotes(section, containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const entries = section?.entries || [];
  if (!entries.length) {
    el.innerHTML = '';
    return;
  }

  const total = section.total_ytd_pnl ?? entries.reduce((sum, item) => sum + safeNum(item.ytd_pnl), 0);
  el.innerHTML = `
    <div class="sectionTitle" style="margin-top:18px">
      <h3>${esc(section.label || '收益明细')}</h3>
      <div class="muted">合计：<span style="color:${safeNum(total) >= 0 ? 'var(--good)' : 'var(--bad)'}">${fmtSignedCny(total)}</span></div>
    </div>
    <div class="muted" style="margin-top:6px">${esc(section.summary_note || '')}</div>
    <div class="tableWrap" style="margin-top:10px">
      <table class="tableWide">
        <thead>
          <tr>
            <th style="width:260px">名称</th>
            <th class="nowrap num" style="width:120px">2026收益</th>
            <th style="width:360px">口径</th>
          </tr>
        </thead>
        <tbody>
          ${entries.map((item) => {
            const pnl = safeNum(item.ytd_pnl);
            const period = item.first_buy && item.last_buy ? `${item.first_buy} 至 ${item.last_buy}` : '';
            const basis = [period, item.basis].filter(Boolean).join('；');
            return `
              <tr>
                <td class="name">${esc(item.name || '')}</td>
                <td class="mono num" style="color:${pnl >= 0 ? 'var(--good)' : 'var(--bad)'}">${fmtSignedCny(pnl)}</td>
                <td>${esc(basis || '-')}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function setKpiFxNote(valueId, fxDailyPnl) {
  const valueEl = document.getElementById(valueId);
  if (!valueEl) return;
  const parent = valueEl.parentElement;
  if (!parent) return;
  let note = parent.querySelector('.kpiFxNote');
  if (!note) {
    note = document.createElement('div');
    note.className = 'kpiFxNote muted';
    parent.appendChild(note);
  }
  const fx = safeNum(fxDailyPnl);
  note.textContent = Math.abs(fx) > 0.005 ? `证券汇兑估算 ${fmtSignedCny(fx)}` : '';
  note.style.color = fx >= 0 ? 'var(--good)' : 'var(--bad)';
}

function applySectionReturnMetrics(resp, ids, options = {}) {
  if (!resp?.summary) return null;
  const totalReturn = safeNum(resp.summary.total_return);
  const totalReturnRate = safeNum(resp.summary.total_return_rate);
  const annualizedRate = safeNum(resp.summary.annualized_return_rate);
  const todayReturn = Number.isFinite(Number(options.dailyPnl))
    ? safeNum(options.dailyPnl)
    : (sectionTodayReturnAmount(resp) ?? 0);
  const fxDailyPnl = safeNum(options.fxDailyPnl);

  setKpiLabel(ids.totalPnlId, '总收益');
  setKpiLabel(ids.dailyPnlId, '今日收益');
  setKpiLabel(ids.returnId, '持仓收益率');

  const totalEl = document.getElementById(ids.totalPnlId);
  const todayEl = document.getElementById(ids.dailyPnlId);
  const rateEl = document.getElementById(ids.returnId);
  if (totalEl) {
    totalEl.textContent = fmtSignedCny(totalReturn);
    totalEl.style.color = totalReturn >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  if (todayEl) {
    todayEl.textContent = fmtSignedCny(todayReturn);
    todayEl.style.color = todayReturn >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  setKpiFxNote(ids.dailyPnlId, fxDailyPnl);
  if (rateEl) {
    rateEl.textContent = fmtSignedPctFromRate(totalReturnRate);
    rateEl.style.color = totalReturnRate >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  return { totalReturn, totalReturnRate, annualizedRate, todayReturn, fxDailyPnl };
}

function applyStaticReturnMetrics(summary, ids, options = {}) {
  if (!summary) return null;
  const totalReturn = safeNum(summary.totalPnl);
  const denominator = safeNum(summary.totalCost) || Math.max(safeNum(summary.totalValue), 0);
  const totalReturnRate = denominator > 0 ? totalReturn / denominator : 0;
  const annualizedRate = options.oncePerYear
    ? totalReturnRate
    : annualizedRateFromPeriod(
      totalReturnRate,
      options.startDate || '2026-01-01',
      options.endDate || '2026-05-31',
    );

  setKpiLabel(ids.totalPnlId, '总收益');
  setKpiLabel(ids.dailyPnlId, '今日收益');
  setKpiLabel(ids.returnId, '持仓收益率');

  const totalEl = document.getElementById(ids.totalPnlId);
  const todayEl = document.getElementById(ids.dailyPnlId);
  const rateEl = document.getElementById(ids.returnId);
  if (totalEl) {
    totalEl.textContent = fmtSignedCny(totalReturn);
    totalEl.style.color = totalReturn >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  if (todayEl) {
    todayEl.textContent = fmtSignedCny(safeNum(summary.dailyPnl));
    todayEl.style.color = safeNum(summary.dailyPnl) >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  if (rateEl) {
    rateEl.textContent = fmtSignedPctFromRate(totalReturnRate);
    rateEl.style.color = totalReturnRate >= 0 ? 'var(--good)' : 'var(--bad)';
  }
  return { totalReturn, totalReturnRate, annualizedRate, todayReturn: safeNum(summary.dailyPnl) };
}

function renderReturnMethodBar() {
  const el = document.getElementById('returnMethodBar');
  if (!el) return;
  el.innerHTML = RETURN_METHODS.map(ReturnMethodBadge).join('');
}

function publishHoldingsKpiSummary(summary, categories) {
  const categoryRows = (categories || []).map((category) => ({
    label: category.label,
    totalValue: safeNum(category.totalValue),
    totalPnl: safeNum(category.totalPnl),
    dailyPnl: safeNum(category.dailyPnl),
    priceDailyPnl: safeNum(category.priceDailyPnl ?? category.dailyPnl),
    fxDailyPnl: safeNum(category.fxDailyPnl),
    dailyTotalPnl: safeNum(category.dailyTotalPnl ?? category.dailyPnl),
  }));
  const authoritative = {
    source: 'holdings_page',
    marketValue: safeNum(summary.marketValue),
    totalPnl: safeNum(summary.totalPnl),
    currentFloatPnl: safeNum(summary.currentFloatPnl),
    dailyPnl: safeNum(summary.dailyPnl),
    dailyPricePnl: categoryRows
      .filter((row) => !isInternalInvestmentLabel(row.label))
      .reduce((sum, row) => sum + row.priceDailyPnl, 0),
    dailyFxPnl: categoryRows
      .filter((row) => !isInternalInvestmentLabel(row.label))
      .reduce((sum, row) => sum + row.fxDailyPnl, 0),
    twr: safeNum(summary.twr),
    asOf: summary.asOf,
    totalLiabilities: safeNum(summary.totalLiabilities),
    cashActiveValue: safeNum(summary.cashActiveValue),
    cashActiveAssets: safeNum(summary.cashActiveAssets),
    cashActiveLiabilities: safeNum(summary.cashActiveLiabilities),
    categories: categoryRows,
  };
  window.__pamsHoldingsKpiSummary = authoritative;
  publishedSummary = authoritative;
  window.dispatchEvent(new CustomEvent('pams:holdings-kpi-ready', { detail: authoritative }));
}

function renderDataQualityPanel(issues, asOf) {
  const el = document.getElementById('investmentDataQualityPanel');
  if (!el) return;
  const rows = [
    { title: '高可信', desc: 'A股、美股等有代码、数量、价格和接口同步记录的资产。', kind: 'ok' },
    { title: '中可信', desc: '基金、理财通和贵金属包含净值或平台收益中心口径，需要和账户核准交叉确认。', kind: 'warn' },
    { title: '待确认', desc: '内部股份、截图收益、手工估值、缺少份额或缺少汇率收益拆分的资产。', kind: 'pending' },
  ];
  el.innerHTML = `
    <div class="qualitySummary">
      <div>${statusBadge('当前评分：中', 'warn')}</div>
      <div class="muted">截至 ${esc(asOf || '待确认')}，投资页优先展示已确认金额；缺字段处不做精确假设。</div>
    </div>
    <div class="qualityGrid">
      ${rows.map(row => `
        <div class="qualityCard">
          ${statusBadge(row.title, row.kind)}
          <p>${esc(row.desc)}</p>
        </div>
      `).join('')}
    </div>
    <div class="issueList">
      ${issues.map(issue => `<div class="issueRow">${statusBadge('需核准', 'pending')}<span>${esc(issue)}</span></div>`).join('')}
    </div>
  `;
}

function renderInvestmentDashboard({
  summary,
  categories,
  contributionRows,
  dataIssues,
  asOf,
  syncAt,
  mobileProfiles,
}) {
  const summaryEl = document.getElementById('investmentSummaryHeader');
  if (summaryEl) summaryEl.innerHTML = InvestmentSummaryHeaderCompact(summary);

  mobileHeroContext = {
    profiles: mobileProfiles || { '全部': { value: summary.marketValue, dailyPnl: summary.dailyPnl, totalPnl: summary.totalPnl, currentFloatPnl: summary.currentFloatPnl, totalRate: summary.twr, share: 1 } },
    dataIssues,
    asOf,
    syncAt,
  };
  updateMobilePortfolioHero();

  const conclusionEl = document.getElementById('investmentConclusionText');
  if (conclusionEl) {
    conclusionEl.classList.remove('skeletonLine');
    conclusionEl.innerHTML = buildInvestmentConclusion(summary, categories, dataIssues);
  }

  const bannerEl = document.getElementById('dataQualityBanner');
  if (bannerEl) {
    const banner = DataQualityBanner(dataIssues);
    bannerEl.style.display = banner ? 'flex' : 'none';
    bannerEl.innerHTML = banner;
  }

  const freshnessEl = document.getElementById('investmentDataFreshness');
  if (freshnessEl) {
    freshnessEl.textContent = `更新 ${formatSyncTimestamp(syncAt, asOf)}`;
    freshnessEl.className = 'statusBadge statusBadge-data';
  }

  void contributionRows;

  const allocationEl = document.getElementById('assetAllocationPanel');
  if (allocationEl) allocationEl.innerHTML = AssetAllocationPanel(categories, summary.marketValue);

  if (document.getElementById('investmentDataQualityPanel')) {
    renderDataQualityPanel(dataIssues, asOf);
  }
}

function AssetAllocationPanel(categories, totalValue) {
  const rows = (categories || [])
    .filter(c => safeNum(c.totalValue) > 0)
    .sort((a, b) => safeNum(b.totalValue) - safeNum(a.totalValue));
  return rows.map(row => {
    const ratio = totalValue > 0 ? safeNum(row.totalValue) / totalValue : 0;
    const dailyPnl = safeNum(row.dailyPnl);
    const totalPnl = safeNum(row.totalPnl);
    return `<div class="allocationRow">
      <div class="allocationMeta">
        <strong>${esc(row.label)}</strong>
        <span>${formatCompactCurrency(row.totalValue)} · ${formatPercentRate(ratio)}</span>
      </div>
      <div class="allocationTrack" aria-hidden="true"><span style="width:${Math.min(100, ratio * 100)}%"></span></div>
      <div class="allocationNumbers">
        <span class="${dailyPnl >= 0 ? 'up' : 'down'}">${fmtSignedCny(dailyPnl)}</span>
        <small class="${totalPnl >= 0 ? 'up' : 'down'}">${fmtSignedCny(totalPnl)}</small>
      </div>
    </div>`;
  }).join('') || '<div class="muted">暂无分布数据</div>';
}

return { statusBadge, ReturnMethodBadge, metricCard, InvestmentSummaryHeader, InvestmentSummaryHeaderCompact, DailyReturnBreakdown, currentMobileHeroGroup, mobileHeroMetaItem, MobilePortfolioHero, updateMobilePortfolioHero, bindMobileMethodology, DataQualityBanner, contributionItem, ContributionList, ProfitContributionPanel, topMoverRow, topMoverGroup, TopContributionPanel, setKpiLabel, renderIbkrPerformance, renderAStockOwnerPerformance, renderRowStock, renderRowFundLike, closedBadge, renderSection, writeSectionFinancials, renderReturnNotes, setKpiFxNote, applySectionReturnMetrics, applyStaticReturnMetrics, renderReturnMethodBar, publishHoldingsKpiSummary, renderDataQualityPanel, renderInvestmentDashboard, AssetAllocationPanel,
  dispose() {
    if (window.__pamsHoldingsKpiSummary === publishedSummary) delete window.__pamsHoldingsKpiSummary;
    mobileHeroContext = null;
  },
};
}
