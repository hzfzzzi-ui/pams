import { accountLabel, fmt2 } from '../../api.js?v=20261031-modules-v1';
import { escapeHtml, formatCurrency, formatPercent, formatPlainMoney as money, formatQuantity, toFiniteNumber as num } from '../../utils.js?v=20261031-modules-v1';

export function createListView({state, scope, legacy}) {
const $ = scope.byId;
const {defaultFilterState, holdingDisplay, isReviewableTrade, option, reviewSummaryParts, reviewSummaryText, sideClass, sideWithStatus, sourceLabel, todayIso} = legacy;
const openBottomSheet = scope.openSheet;
const closeBottomSheet = scope.closeSheet;

function fillSelects() {
  $('tradeAccount').innerHTML = state.accounts
    .map((a) => option(a.account_code, a.display_name || accountLabel(a.account_code)))
    .join('');

  const preferred = state.accounts.find((a) => a.account_code === 'tencent_licaitong') || state.accounts[0];
  if (preferred) $('tradeAccount').value = preferred.account_code;
}

function assetFilterOptions(selected) {
  return [
    '<option value="">全部标的</option>',
    ...state.assets.map((asset) => {
      const name = asset.display_name || asset.name || asset.symbol || asset.asset_key || '未命名标的';
      const symbol = asset.symbol && asset.symbol !== name ? ` · ${asset.symbol}` : '';
      const pending = Number(asset.pending_count || 0);
      const total = Number(asset.trade_count || 0);
      const suffix = pending > 0 ? ` · 待 ${pending}/${total}` : ` · 共 ${total}`;
      return option(asset.asset_key, `${name}${symbol}${suffix}`, selected === asset.asset_key);
    }),
  ].join('');
}

function hasActiveTradeFilters() {
  const defaults = defaultFilterState();
  const filters = state.filters || defaults;
  return filters.start !== defaults.start
    || filters.end !== defaults.end
    || Boolean(filters.side)
    || Boolean(filters.assetKey)
    || Boolean(filters.search);
}

function TradeFilterSheet() {
  const filters = state.filters || defaultFilterState();
  return `
    <form id="tradeFilterForm" class="tradeFilterSheetForm">
      <div class="tradeFilterSheetGrid">
        <label class="formLabel" for="filterSheetStart">开始日期</label>
        <input id="filterSheetStart" class="formInput" type="date" value="${escapeHtml(filters.start || '')}" />

        <label class="formLabel" for="filterSheetEnd">结束日期</label>
        <input id="filterSheetEnd" class="formInput" type="date" value="${escapeHtml(filters.end || '')}" />

        <label class="formLabel" for="filterSheetAsset">交易标的</label>
        <select id="filterSheetAsset" class="formInput">${assetFilterOptions(filters.assetKey || '')}</select>

        <label class="formLabel" for="filterSheetSearch">关键词</label>
        <input id="filterSheetSearch" class="formInput" type="search" value="${escapeHtml(filters.search || '')}" placeholder="代码 / 名称 / 备注" />

        <label class="formLabel" for="filterSheetSide">方向</label>
        <select id="filterSheetSide" class="formInput">
          ${option('', '全部', !filters.side)}
          ${option('buy', '买入', filters.side === 'buy')}
          ${option('sell', '卖出', filters.side === 'sell')}
          ${option('dividend', '分红', filters.side === 'dividend')}
          ${option('deposit', '入金', filters.side === 'deposit')}
          ${option('withdrawal', '出金', filters.side === 'withdrawal')}
          ${option('fx_conversion', '换汇', filters.side === 'fx_conversion')}
        </select>
      </div>
      <div class="tradeFilterSheetActions">
        <button id="btnClearTradeFilters" class="btn" type="button">重置</button>
        <button class="btn primary" type="submit">应用筛选</button>
      </div>
    </form>
  `;
}

function readTradeFilterForm(form) {
  return {
    start: form.querySelector('#filterSheetStart')?.value || '',
    end: form.querySelector('#filterSheetEnd')?.value || '',
    side: form.querySelector('#filterSheetSide')?.value || '',
    assetKey: form.querySelector('#filterSheetAsset')?.value || '',
    search: form.querySelector('#filterSheetSearch')?.value?.trim() || '',
  };
}

function fillTradeNameOptions(rows) {
  const datalist = $('tradeNameList');
  if (!datalist) return;
  const seen = new Set();
  datalist.innerHTML = (rows || [])
    .filter((r) => r?.name || r?.symbol)
    .map((r) => {
      const value = r.name || r.symbol;
      const key = String(value).trim();
      if (!key || seen.has(key)) return '';
      seen.add(key);
      const label = [r.symbol, r.account_name || accountLabel(r.account_code)].filter(Boolean).join(' · ');
      return `<option value="${escapeHtml(value)}"${label ? ` label="${escapeHtml(label)}"` : ''}></option>`;
    })
    .filter(Boolean)
    .join('');
}

function setDefaultFilters() {
  state.filters = defaultFilterState();
  $('tradeDate').value = todayIso();
  state.showAll = false;
}

function setTradeFilterPanelOpen(open, opts = {}) {
  const button = $('btnToggleTradeFilters');
  if (!open) {
    closeBottomSheet();
    button?.setAttribute('aria-expanded', 'false');
    button?.classList.toggle('active', hasActiveTradeFilters());
    return;
  }
  openBottomSheet('筛选', TradeFilterSheet());
  button?.setAttribute('aria-expanded', 'true');
  button?.classList.add('active');
  if (opts.focus !== false) {
    scope.frame(() => {
      ($('filterSheetAsset') || $('filterSheetSearch'))?.focus?.({ preventScroll: true });
    });
  }
}

function toggleTradeFilterPanel() {
  setTradeFilterPanelOpen(true, { focus: true });
}

function buildQuery() {
  const params = new URLSearchParams();
  const filters = state.filters || defaultFilterState();
  const start = filters.start;
  const end = filters.end;
  const side = filters.side;
  const assetKey = filters.assetKey;
  const search = filters.search;
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  if (side) params.set('side', side);
  if (assetKey) params.set('asset_key', assetKey);
  if (search) params.set('search', search);
  params.set('limit', '1000');
  return params.toString();
}

function renderSummary(summary) {
  if ($('kpiCount')) $('kpiCount').textContent = summary?.count ?? 0;
  if ($('kpiBuy')) $('kpiBuy').textContent = formatCurrency(summary?.buy_amount_personal_cny ?? summary?.buy_amount_cny ?? 0);
  if ($('kpiSell')) $('kpiSell').textContent = formatCurrency(summary?.sell_amount_personal_cny ?? summary?.sell_amount_cny ?? 0);
  if ($('kpiFees')) $('kpiFees').textContent = formatCurrency(summary?.fees_personal_cny ?? summary?.fees_cny ?? 0);
  if ($('ledgerMeta')) $('ledgerMeta').textContent = (summary?.sources || []).map(sourceLabel).join(' / ') || '--';
}

function TransactionSummaryPanel(summary, rows) {
  const buyRaw = num(summary?.buy_amount_cny);
  const sellRaw = num(summary?.sell_amount_cny);
  const feesRaw = num(summary?.fees_cny);
  const buy = num(summary?.buy_amount_personal_cny ?? summary?.buy_amount_cny);
  const sell = num(summary?.sell_amount_personal_cny ?? summary?.sell_amount_cny);
  const fees = num(summary?.fees_personal_cny ?? summary?.fees_cny);
  const turnover = buy + sell;
  const feeRate = turnover > 0 ? fees / turnover : null;
  const ownerAdjusted = !!summary?.has_owner_adjustments;
  const allRows = rows || [];
  const visibleRows = state.showAll ? allRows : allRows.slice(0, 10);
  const hiddenCount = Math.max(0, allRows.length - visibleRows.length);
  const sourceText = (summary?.sources || []).map(sourceLabel).filter(Boolean).join(' / ') || '暂无来源';
  const filters = state.filters || defaultFilterState();
  const rangeText = [
    filters.start || '不限开始',
    filters.end || '不限结束',
  ].join(' 至 ');
  const filterActive = hasActiveTradeFilters();
  return `
    <div class="transactionOverview">
      <div class="transactionOverviewHead">
        <div>
          <span>筛选记录</span>
          <strong>${summary?.count ?? 0}</strong>
          <small>${escapeHtml(rangeText)}</small>
        </div>
      </div>
      <div class="transactionOverviewMetrics">
        <div>
          <span>个人买入</span>
          <strong>${formatCurrency(buy)}</strong>
          <small>${ownerAdjusted ? `账户原始 ${formatCurrency(buyRaw)}` : '与账户原始一致'}</small>
        </div>
        <div>
          <span>个人卖出</span>
          <strong>${formatCurrency(sell)}</strong>
          <small>${ownerAdjusted ? `账户原始 ${formatCurrency(sellRaw)}` : '与账户原始一致'}</small>
        </div>
        <div class="${feeRate != null && feeRate > 0.01 ? 'warn' : ''}">
          <span>个人税费</span>
          <strong>${feeRate == null ? '无法计算' : formatPercent(feeRate)}</strong>
          <small>${formatCurrency(fees)} · 账户原始 ${formatCurrency(feesRaw)}</small>
        </div>
      </div>
    </div>
    <div class="recentTradePanel">
      <div class="sectionTitle">
        <h3>交易明细</h3>
        <div class="tradeListActions">
          <button id="btnOpenTradeForm" class="btn primary" type="button">手动录入</button>
          <button id="btnToggleTradeFilters" class="btn iconBtn tradeFilterToggle${filterActive ? ' active' : ''}" type="button" aria-label="筛选" aria-controls="tradeFilterPanel" aria-expanded="false" title="筛选">
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M4 6h16l-6.2 7.2v4.9l-3.6 1.9v-6.8L4 6z"></path>
            </svg>
          </button>
        </div>
      </div>
      <div class="muted">数据来源：${escapeHtml(sourceText)}</div>
      <div class="recentTradeList">
        ${visibleRows.length ? visibleRows.map((r) => {
          const holding = holdingDisplay(r);
          const price = r.price ? `成交价 ${fmt2(r.price)}` : '';
          const account = r.account_name || accountLabel(r.account_code);
          const reviewable = isReviewableTrade(r);
          const reviewText = reviewSummaryText(r);
          const hasReview = reviewSummaryParts(r).length > 0;
          const ownerRatio = num(r.owner_ratio ?? 1);
          const personalLine = ownerRatio !== 1 && r.gross_amount_personal_cny != null
            ? `个人口径 ${formatCurrency(r.gross_amount_personal_cny)} · 税费 ${formatCurrency(r.fees_personal_cny || 0)}`
            : '';
          return `
          <div class="recentTradeItem tradeLedgerItem">
            <div class="tradeItemMain">
              <span class="mono tradeItemDate">${escapeHtml(r.trade_date || '')}</span>
              <strong>${escapeHtml(holding.name)}</strong>
              <small>${escapeHtml([holding.meta, price, account].filter(Boolean).join(' · '))}</small>
              <small class="tradeReviewSummary ${hasReview ? 'hasReview' : ''}">${escapeHtml(reviewText)}</small>
            </div>
            <div class="tradeItemNumbers">
              <span class="sideTag ${sideClass(r.side_normalized)}">${escapeHtml(sideWithStatus(r.side_normalized, r.status))}</span>
              <strong class="mono">${money(r.gross_amount, r.currency)}</strong>
              <small>份数 ${escapeHtml(formatQuantity(r.quantity))} · 税费 ${escapeHtml(money(r.fees, r.currency))}</small>
              ${personalLine ? `<small>${escapeHtml(personalLine)}</small>` : ''}
              ${reviewable ? `<button type="button" class="tradeReviewBtn" data-trade-review="${escapeHtml(String(r.native_id))}">${hasReview ? '已标注' : '补复盘'}</button>` : ''}
            </div>
          </div>
        `;
        }).join('') : '<div class="emptyState">当前筛选下没有交易记录。</div>'}
      </div>
      ${allRows.length > 10 ? `
        <button id="btnToggleAllTrades" class="tradeListMoreBtn" type="button">
          ${state.showAll ? '收起' : `展开更多（剩余 ${hiddenCount} 单）`}
        </button>
      ` : ''}
    </div>
  `;
}

function renderTransactionSummary(summary, rows) {
  const el = $('transactionSummaryPanel');
  if (!el) return;
  el.innerHTML = TransactionSummaryPanel(summary || {}, rows || []);
}

return {fillSelects, assetFilterOptions, hasActiveTradeFilters, TradeFilterSheet, readTradeFilterForm, fillTradeNameOptions, setDefaultFilters, setTradeFilterPanelOpen, toggleTradeFilterPanel, buildQuery, renderSummary, TransactionSummaryPanel, renderTransactionSummary};
}
