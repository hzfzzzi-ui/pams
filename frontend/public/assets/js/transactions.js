import { accountLabel, fmt2, jget, jpost } from './api.js?v=20260619-investment-hardening-v1';
import { closeBottomSheet, openBottomSheet, setActiveNav } from './ui.js?v=20260614-trade-mode-attribution-v12';
import {
  byId as $,
  escapeHtml,
  formatCurrency,
  formatPercent,
  formatPlainMoney as money,
  formatQuantity,
  clearError,
  readOptionalNumber,
  showError,
  toFiniteNumber as num,
} from './utils.js?v=20260607-shared-format-v1';

const state = {
  accounts: [],
  categories: [],
  sources: [],
  assets: [],
  reviewOptions: {},
  filters: defaultFilterState(),
  rows: [],
  summary: {},
  attribution: null,
  campaigns: null,
  showAll: false,
};

const REVIEW_TAG_GROUP_LABELS = {
  execution: '执行辅助标签（最多 3 个）',
  behavior: '纪律 / 行为标签',
  data: '数据 / 非投资标签',
  post_review: '事后复盘标签',
};

const HOLDING_PERIOD_OPTIONS = [
  { value: '1周内', label: '1周内' },
  { value: '1个月内', label: '1个月内' },
  { value: '3个月', label: '3个月' },
  { value: '6-12个月', label: '6-12个月' },
  { value: '长期', label: '长期' },
  { value: '事件兑现前', label: '事件兑现前' },
  { value: '复盘日前', label: '复盘日前' },
];

const POSITION_IDENTITY_RULES = {
  allocation: {
    maxPositionPct: '20',
    sellRuleType: 'allocation_rebalance',
    sellCondition: '再平衡 / 目标比例偏离。',
    holdingPeriod: '长期',
    hint: '配置仓适合组合底仓，重点看目标比例偏离和再平衡纪律。',
  },
  value: {
    maxPositionPct: '12',
    sellRuleType: 'value_thesis',
    sellCondition: '基本面证伪 / 估值过高 / 安全边际消失。',
    holdingPeriod: '6-12个月',
    hint: '价值仓需要写清证伪条件，避免亏损后改口径。',
  },
  trend: {
    maxPositionPct: '8',
    sellRuleType: 'trend_exit',
    sellCondition: '趋势破坏 / 止损 / 移动止盈。',
    holdingPeriod: '1个月内',
    hint: '趋势仓必须尊重退出信号，不应跌后改成价值仓。',
  },
  event: {
    maxPositionPct: '6',
    sellRuleType: 'event_resolution',
    sellCondition: '事件兑现 / 事件失败 / 催化消失。',
    holdingPeriod: '事件兑现前',
    hint: '事件仓到事件结束就复盘，不能自然漂移成长期仓。',
  },
  trial: {
    maxPositionPct: '2',
    sellRuleType: 'trial_review',
    sellCondition: '到期复盘 / 小仓位 / 不允许无限加仓。',
    holdingPeriod: '复盘日前',
    hint: '试验仓只能小仓位验证假设，重新分类前不继续加仓。',
  },
  cash_management: {
    maxPositionPct: '30',
    sellRuleType: 'cash_need',
    sellCondition: '现金需求 / 收益率替代 / 风险变化。',
    holdingPeriod: '1个月内',
    hint: '现金管理仓看流动性和替代收益率，不和风险资产混用口径。',
  },
  uncertain: {
    maxPositionPct: '1.5',
    sellRuleType: 'uncertain_reclassify',
    sellCondition: '复盘日前重新分类；重新分类前不允许继续加仓。',
    holdingPeriod: '复盘日前',
    hint: '不确定可以，但必须小仓位、限期复盘、不能无限加仓。',
  },
  closed: {
    maxPositionPct: '',
    sellRuleType: 'closed',
    sellCondition: '仓位已关闭。',
    holdingPeriod: '',
    hint: '已关闭仓位主要填写事后复盘和身份变化原因。',
  },
};

const COMMON_REVIEW_TAG_VALUES = new Set([
  'chase_high',
  'early_bottom',
  'late_confirmation',
  'sold_too_early',
  'sold_too_late',
  'oversized',
  'insufficient_batch',
  'theme_concentration',
  'ignored_stop_loss',
  'plan_followed',
  'bad_discipline',
  'fomo',
  'panic',
  'anchored_cost',
  'overconfidence',
  'hesitation',
  'loss_rationalization',
  'thesis_correct',
  'thesis_wrong',
  'right_reason_poor_execution',
  'right_result_weak_reason',
  'identity_correct',
  'identity_wrong',
  'identity_change_reasonable',
  'identity_drift',
]);

const REVIEW_TAG_LABEL_OVERRIDES = {
  early_bottom: '买早了',
  late_confirmation: '买晚了',
};

const REVIEW_VALUE_LABELS = {
  planned_take_profit: '计划止盈 / 目标达成',
  risk_control_stop_loss: '风险控制 / 止损',
  thesis_invalidated: '逻辑证伪 / 基本面变化',
  trend_signal_exit: '趋势转弱 / 信号退出',
  risk_reduction_rebalance: '风险降仓 / 再平衡',
  event_completed: '事件兑现 / 催化结束',
  replacement_better_asset: '换仓 / 更优标的替代',
  liquidity_need: '资金需求',
  correction: '误操作 / 修正',
  allocation: '配置仓',
  value: '价值仓',
  trend: '趋势仓',
  event: '事件仓',
  trial: '试验仓 / 观察仓',
  cash_management: '现金管理仓',
  uncertain: '未确定 / 待判断',
  closed: '已关闭',
  trend_entry_failed: '趋势买入失效',
  early_bottom_no_validation: '抄底未验证',
  add_after_drop_failed: '补仓未验证',
  stop_loss_not_executed: '止损未执行',
  closed_stop_loss_buy: '亏损已止损',
  risk_reduction_cost: '降风险机会成本',
  profit_take_too_early: '止盈过早',
  stop_loss_effective: '退出纪律有效',
  rebalance_effective: '降风险有效',
};

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function defaultFilterState() {
  return {
    start: '',
    end: todayIso(),
    side: '',
    assetKey: '',
    search: '',
  };
}

function holdingDisplay(row) {
  const symbol = row?.symbol || '';
  const name = row?.name || symbol || '未命名标的';
  const category = [row?.major_category, row?.minor_category].filter(Boolean).join(' / ');
  return {
    symbol,
    name,
    meta: [symbol, category].filter(Boolean).join(' · '),
  };
}

function sideLabel(side) {
  const normalized = String(side || '').toLowerCase();
  const labels = {
    buy: '买入',
    sell: '卖出',
    dividend: '分红',
    deposit: '入金',
    cash_deposit: '入金',
    withdraw: '出金',
    withdrawal: '出金',
    cash_withdrawal: '出金',
    transfer: '转换',
    transfer_in: '转入',
    transfer_out: '转出',
    fx_conversion: '换汇',
    fee: '费用',
    other: '其他',
  };
  return labels[normalized] || side || '其他';
}

function sideClass(side) {
  const normalized = String(side || '').toLowerCase();
  if (['buy', 'deposit', 'cash_deposit', 'transfer_in'].includes(normalized)) return 'buy';
  if (['sell', 'withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out'].includes(normalized)) return 'sell';
  if (normalized === 'dividend') return 'dividend';
  if (normalized === 'fx_conversion') return 'other';
  return 'other';
}

function sourceLabel(source) {
  const labels = {
    manual: '手动',
    manual_eod: '每日复核',
    tencent_licaitong: '理财通',
    tradingview: 'TradingView',
    ibkr: 'IBKR',
  };
  return labels[source] || source || '';
}

function reviewOptionList(key) {
  const value = state.reviewOptions?.[key];
  return Array.isArray(value) ? value : [];
}

function reviewOptionLabel(key, value) {
  const normalized = String(value || '').trim();
  if (!normalized) return '';
  return reviewOptionList(key).find((item) => item.value === normalized)?.label || normalized;
}

function reviewStrictOptionLabel(key, value) {
  const normalized = String(value || '').trim();
  if (!normalized) return '';
  return reviewOptionList(key).find((item) => item.value === normalized)?.label || '';
}

function isReviewableTrade(row) {
  return String(row?.row_id || '').startsWith('fact-') && row?.native_id != null;
}

function reviewSummaryParts(row) {
  const parts = [];
  const side = String(row?.side_normalized || '').toLowerCase();
  const identity = row?.current_position_identity || row?.initial_position_identity;
  if (identity) {
    parts.push(`身份：${reviewOptionLabel('current_position_identities', identity) || reviewOptionLabel('position_identities', identity)}`);
  }
  if (side === 'buy' && row?.buy_reason) {
    parts.push(`买入：${reviewOptionLabel('buy_reasons', row.buy_reason)}`);
  }
  if (side === 'sell' && row?.sell_reason) {
    parts.push(`卖出：${reviewOptionLabel('sell_reasons', row.sell_reason)}`);
  }
  const tags = Array.isArray(row?.review_tags) ? row.review_tags : [];
  if (tags.length) {
    parts.push(tags.slice(0, 2).map((tag) => reviewOptionLabel('review_tags', tag)).join(' / '));
  }
  return parts;
}

function reviewSummaryText(row) {
  const parts = reviewSummaryParts(row);
  return parts.length ? parts.join(' · ') : '未补充复盘标注';
}

function formatPctPoint(value, digits = 2) {
  if (value == null || value === '') return '--';
  const n = Number(value);
  if (!Number.isFinite(n)) return '--';
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(digits)}%`;
}

function pctTone(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  if (n > 0.01) return 'good';
  if (n < -0.01) return 'bad';
  return 'neutral';
}

function autoConfidenceLabel(value) {
  const labels = {
    high: '高置信',
    medium: '中置信',
    low: '低置信',
  };
  return labels[String(value || '')] || '低置信';
}

function autoLevelTone(level) {
  const value = String(level || '').toLowerCase();
  if (value === 'good') return 'good';
  if (value === 'bad' || value === 'danger') return 'bad';
  if (value === 'warn') return 'warn';
  if (value === 'muted') return 'muted';
  return 'neutral';
}

function autoReviewTagLabel(tag) {
  return reviewOptionLabel('review_tags', tag);
}

function autoSuggestedTagButtons(tags) {
  const values = (Array.isArray(tags) ? tags : []).filter(Boolean).slice(0, 3);
  if (!values.length) return '';
  return `
    <div class="tradeAutoSuggestionBar">
      <span>建议标签</span>
      ${values.map((tag) => `
        <button type="button" class="tradeAutoSuggestion" data-auto-tag="${escapeHtml(tag)}">
          ${escapeHtml(autoReviewTagLabel(tag))}
        </button>
      `).join('')}
    </div>
  `;
}

function sideWithStatus(side, status) {
  return sideLabel(side);
}

function option(value, label, selected = false) {
  return `<option value="${escapeHtml(value)}"${selected ? ' selected' : ''}>${escapeHtml(label)}</option>`;
}

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

function resolveManualAsset(name) {
  const needle = String(name || '').trim().toLowerCase();
  if (!needle) return {};
  return state.rows.find((r) => {
    return String(r?.name || '').trim().toLowerCase() === needle
      || String(r?.symbol || '').trim().toLowerCase() === needle;
  }) || {};
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
    requestAnimationFrame(() => {
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

function reviewAttributionLabel(value) {
  const key = String(value || '');
  return reviewStrictOptionLabel('buy_reasons', key)
    || reviewStrictOptionLabel('sell_reasons', key)
    || reviewStrictOptionLabel('current_position_identities', key)
    || reviewStrictOptionLabel('position_identities', key)
    || reviewStrictOptionLabel('review_tags', key)
    || REVIEW_VALUE_LABELS[key]
    || key
    || '未标注';
}

function AttributionBucketList(title, rows, emptyText) {
  const visible = (rows || []).slice(0, 4);
  return `
    <div class="tradeAttributionBucket">
      <div class="tradeAttributionBucketHead">
        <strong>${escapeHtml(title)}</strong>
        <span>${visible.length ? `${visible.length} 项` : '暂无'}</span>
      </div>
      <div class="tradeAttributionRows">
        ${visible.length ? visible.map((row) => `
          <div class="tradeAttributionRow">
            <div>
              <strong>${escapeHtml(reviewAttributionLabel(row.key || row.label))}</strong>
              <small>${escapeHtml(row.quality === 'low_sample' ? '样本不足，仅供参考' : `样本 ${row.sample_count || 0}`)}</small>
            </div>
            <div class="tradeAttributionMetric ${pctTone(row.avg_return_pct)}">
              ${escapeHtml(formatPctPoint(row.avg_return_pct))}
            </div>
          </div>
        `).join('') : `<div class="emptyState">${escapeHtml(emptyText)}</div>`}
      </div>
    </div>
  `;
}

function TradeAttributionPanel(payload) {
  if (!payload) {
    return `
      <div id="tradeAttributionPanel" class="tradeAttributionPanel">
        <div class="muted">正在计算自动归因...</div>
      </div>
    `;
  }
  const groups = payload.groups || {};
  const firstEvent = (payload.events || [])[0];
  return `
    <div id="tradeAttributionPanel" class="tradeAttributionPanel">
      <div class="sectionTitle">
        <h3>自动归因</h3>
        <div class="muted">事件级近似 · ${escapeHtml(payload.as_of || '--')}</div>
      </div>
      <div class="tradeAttributionStats">
        <div><span>有效样本</span><strong>${payload.sample_count || 0}</strong></div>
        <div><span>缺价格</span><strong>${payload.missing_price_count || 0}</strong></div>
        <div><span>归因口径</span><strong>P0</strong></div>
      </div>
      <div class="tradeAttributionGrid">
        ${AttributionBucketList('买入原因', groups.buy_reason, '补入买入原因后自动统计。')}
        ${AttributionBucketList('卖出原因', groups.sell_reason, '补入卖出原因后自动统计。')}
        ${AttributionBucketList('行为标签', groups.review_tags, '行为标签是多选口径，不可加总。')}
      </div>
      <div class="tradeAttributionLatest">
        <span>${firstEvent ? escapeHtml(firstEvent.trade_date || '') : '等待样本'}</span>
        <strong>${firstEvent ? escapeHtml(firstEvent.name || firstEvent.symbol || '--') : '暂无可归因交易'}</strong>
        <em class="${firstEvent ? pctTone(firstEvent.current_return_pct) : ''}">
          ${firstEvent ? escapeHtml(formatPctPoint(firstEvent.current_return_pct)) : '--'}
        </em>
      </div>
      <div class="muted tradeAttributionNote">${escapeHtml(payload.data_quality?.note || '事件级近似，未做精确 lot matching。')}</div>
    </div>
  `;
}

function attributionBucketMeta(row, mode) {
  const sample = Number(row?.sample_count || 0);
  if (mode === 'sell') {
    return `样本 ${sample} · 卖飞 ${row?.missed_gain_count || 0} · 避亏 ${row?.avoided_loss_count || 0}`;
  }
  if (mode === 'identity') {
    return row?.quality === 'low_sample' ? `样本 ${sample} · 样本不足` : `样本 ${sample}`;
  }
  return row?.quality === 'low_sample' ? `样本 ${sample} · 仅参考` : `样本 ${sample}`;
}

function AttributionBucketListV2(title, rows, mode, emptyText) {
  const visible = (rows || []).slice(0, 3);
  return `
    <div class="tradeAttributionBucket tradeAttributionBucketV2">
      <div class="tradeAttributionBucketHead">
        <strong>${escapeHtml(title)}</strong>
        <span>${visible.length ? `${visible.length} 项` : '暂无'}</span>
      </div>
      <div class="tradeAttributionRows">
        ${visible.length ? visible.map((row) => `
          <div class="tradeAttributionRow tradeAttributionRowV2">
            <div>
              <strong>${escapeHtml(reviewAttributionLabel(row.key || row.label))}</strong>
              <small>${escapeHtml(attributionBucketMeta(row, mode))}</small>
            </div>
            <div class="tradeAttributionMetric ${pctTone(row.avg_return_pct)}">
              <span>${escapeHtml(mode === 'sell' ? '卖后' : '买后')}</span>
              ${escapeHtml(formatPctPoint(row.avg_return_pct))}
            </div>
          </div>
        `).join('') : `<div class="emptyState">${escapeHtml(emptyText)}</div>`}
      </div>
    </div>
  `;
}

function campaignStatusLabel(status) {
  const labels = {
    open: '仍在持有',
    closed: '已清仓',
    amount_only: '缺少份额',
    orphan_sell: '卖出待核对',
  };
  return labels[String(status || '')] || status || '--';
}

function campaignStatusTone(status) {
  if (status === 'closed') return 'good';
  if (status === 'amount_only' || status === 'orphan_sell') return 'warn';
  return 'data';
}

function campaignCurrencyLabel(currency) {
  const labels = {
    CNY: '人民币',
    USD: '美元',
    HKD: '港币',
    KRW: '韩元',
    MXN: '墨西哥比索',
  };
  return labels[String(currency || '').toUpperCase()] || currency || '原币';
}

function formatCampaignMoney(value, currency) {
  if (value == null || value === '') return '--';
  const n = Number(value);
  if (!Number.isFinite(n)) return '--';
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}${money(Math.abs(n), currency || '')}`;
}

function formatCampaignQuantity(value) {
  if (value == null || value === '') return '--';
  const n = Number(value);
  if (!Number.isFinite(n)) return '--';
  return formatQuantity(n, { maximumFractionDigits: 2 });
}

function campaignPeriod(campaign) {
  const start = campaign.start_date || '--';
  const end = campaign.end_date || campaign.latest_trade_date || '--';
  return start === end ? start : `${start} 至 ${end}`;
}

function campaignHint(campaign) {
  if (campaign.status === 'amount_only') return '这类交易缺少份额，只能按金额汇总，暂不能严格拆分每次卖出成本。';
  if (campaign.status === 'orphan_sell') return '有卖出记录没有匹配到前序买入，需要回看原始交易。';
  if (!Number(campaign.sell_count || 0)) return '还没有卖出，所以这里的已实现盈亏为 0；当前浮盈亏看持仓页。';
  if (campaign.status === 'open') return '已经卖出的部分计入已实现盈亏，剩余持仓的浮盈亏仍看持仓页。';
  return '这一轮已经清仓，金额表示本轮卖出后的已实现盈亏。';
}

function CampaignLifecyclePanel(payload) {
  if (!payload) {
    return `
      <section class="tradeCampaignPanel">
        <div class="muted">正在整理卖出复盘...</div>
      </section>
    `;
  }
  const summary = payload.summary || {};
  const campaigns = payload.campaigns || [];
  const statusCounts = summary.status_counts || {};
  const visibleTotals = Object.entries(summary.currency_totals || {}).slice(0, 4);
  return `
    <section class="tradeCampaignPanel">
      <div class="tradeCampaignHero">
        <div>
          <span class="tradeCampaignEyebrow">交易归因</span>
          <strong>卖出复盘</strong>
          <p>把同一账户里的同一标的串成一轮持仓，先看已经卖出/赎回的盈亏；还没卖出的浮盈亏仍以持仓页为准。</p>
        </div>
        <div class="tradeCampaignTotal">
          <span>识别周期</span>
          <strong>${escapeHtml(summary.total_campaigns ?? 0)}</strong>
          <em>轮</em>
        </div>
      </div>
      ${visibleTotals.length ? `
        <div class="tradeCampaignRealized">
          <div class="tradeCampaignRealizedHead">
            <strong>已实现盈亏</strong>
            <span>按原币种统计，只包含卖出/赎回部分</span>
          </div>
          <div class="tradeCampaignCurrency">
            ${visibleTotals.map(([currency, totals]) => `
              <div>
                <span>${escapeHtml(campaignCurrencyLabel(currency))}</span>
                <strong class="${pctTone(totals.realized_pnl_estimated)}">${escapeHtml(formatCampaignMoney(totals.realized_pnl_estimated, currency))}</strong>
              </div>
            `).join('')}
          </div>
        </div>
      ` : ''}
      <div class="tradeCampaignStats">
        <div><span>仍在持有</span><strong>${escapeHtml(statusCounts.open || 0)}</strong><small>有剩余份额</small></div>
        <div><span>已清仓</span><strong>${escapeHtml(statusCounts.closed || 0)}</strong><small>完整闭环</small></div>
        <div><span>缺少份额</span><strong>${escapeHtml(statusCounts.amount_only || 0)}</strong><small>只能按金额看</small></div>
      </div>
      <div class="tradeCampaignListHead">
        <strong>最近有动作的周期</strong>
        <span>每行是一只标的一轮建仓、加仓、减仓或清仓</span>
      </div>
      <div class="tradeCampaignRows">
        ${campaigns.length ? campaigns.slice(0, 3).map((campaign) => {
          const currency = campaign.currency || '';
          const tone = campaignStatusTone(campaign.status);
          return `
            <article class="tradeCampaignRow tradeCampaignRow-${tone}">
              <div class="tradeCampaignRowHead">
                <div>
                  <strong>${escapeHtml(campaign.name || campaign.symbol || '--')}</strong>
                  <small>周期：${escapeHtml(campaignPeriod(campaign))}</small>
                </div>
                <span class="tradeCampaignStatus ${tone}">${escapeHtml(campaignStatusLabel(campaign.status))}</span>
              </div>
              <div class="tradeCampaignFacts">
                <div><span>交易</span><strong>买 ${escapeHtml(campaign.buy_count || 0)} / 卖 ${escapeHtml(campaign.sell_count || 0)}</strong></div>
                <div><span>剩余份额</span><strong>${escapeHtml(formatCampaignQuantity(campaign.open_quantity))}</strong></div>
                <div><span>已实现盈亏</span><strong class="${pctTone(campaign.realized_pnl_estimated)}">${escapeHtml(formatCampaignMoney(campaign.realized_pnl_estimated, currency))}</strong></div>
              </div>
              <p>${escapeHtml(campaignHint(campaign))}</p>
            </article>
          `;
        }).join('') : '<div class="emptyState">暂无仓位周期样本。</div>'}
      </div>
      <div class="muted tradeAttributionNote">说明：这里不是当前持仓浮盈亏；成本按先进先出近似配对。缺少份额的交易只按金额归组，不参与严格配对。</div>
    </section>
  `;
}

function workbenchTone(level) {
  if (level === 'danger' || level === 'bad') return 'bad';
  if (level === 'warn' || level === 'muted') return 'warn';
  if (level === 'good') return 'good';
  return 'neutral';
}

function formatCnyEffect(value) {
  if (value == null || value === '') return '--';
  return formatCampaignMoney(value, 'CNY');
}

function formatCnyEffectCompact(value) {
  const numeric = tradeReviewNumber(value, 0);
  if (!numeric) return '¥0';
  const sign = numeric > 0 ? '+' : '-';
  const abs = Math.abs(numeric);
  const compact = (n, digits = 0) => n.toLocaleString('zh-CN', {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  });
  if (abs >= 100000000) return `${sign}¥${compact(abs / 100000000, 1)}亿`;
  if (abs >= 10000) return `${sign}¥${compact(abs / 10000, abs >= 100000 ? 1 : 2)}万`;
  if (abs >= 1000) return `${sign}¥${compact(abs, 0)}`;
  if (abs >= 100) return `${sign}¥${compact(abs, 0)}`;
  return `${sign}¥${compact(abs, abs >= 10 ? 1 : 2)}`;
}

function tradeReviewNumber(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function tradeReviewHeatTone(value) {
  const n = tradeReviewNumber(value, 0);
  if (n > 0.01) return 'gain';
  if (n < -0.01) return 'loss';
  return 'flat';
}

const TRADE_REVIEW_ACTION_PATTERN_KEYS = new Set(['review_tags', 'buy_reason', 'sell_reason']);

function tradeReviewActionPatterns(patterns) {
  return (patterns || []).filter((group) => TRADE_REVIEW_ACTION_PATTERN_KEYS.has(group?.key));
}

function formatDecisionRate(value) {
  const text = formatPctPoint(value, 1);
  return text.startsWith('+') ? text.slice(1) : text;
}

function tradeReviewAssetName(asset) {
  const name = String(asset?.name || asset?.symbol || asset?.key || '').trim();
  const symbol = String(asset?.symbol || '').trim();
  if (!name) return symbol || '--';
  if (symbol && name.toUpperCase().startsWith(`${symbol.toUpperCase()} - `)) return symbol;
  if (symbol && name === symbol) return symbol;
  return name;
}

function tradeReviewAssetMeta(asset) {
  const name = String(asset?.name || '').trim();
  const symbol = String(asset?.symbol || '').trim();
  if (symbol && name && name !== symbol && !name.toUpperCase().startsWith(`${symbol.toUpperCase()} - `)) {
    return symbol;
  }
  if (symbol && name.includes(' - ')) return name.split(' - ').slice(1).join(' - ');
  return symbol || name || '--';
}

function tradeReviewAssetCleanName(asset) {
  const name = String(asset?.name || '').trim();
  const symbol = String(asset?.symbol || '').trim();
  if (!name) return symbol || '--';
  if (symbol && name.toUpperCase().startsWith(`${symbol.toUpperCase()} - `)) {
    return name.slice(symbol.length + 3).trim() || symbol;
  }
  return name;
}

function tradeReviewCompactSymbol(symbol) {
  const value = String(symbol || '').trim();
  if (!value) return '';
  if (value.length <= 10) return value;
  const parts = value.replace(/[_-]+/g, ' ').split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return parts.slice(0, 2).join(' ');
  return value.slice(0, 10);
}

function tradeReviewAssetShortLabel(asset) {
  const symbol = String(asset?.symbol || '').trim();
  const cleanName = tradeReviewAssetCleanName(asset);
  if (symbol && /^[A-Z0-9._-]+$/i.test(symbol)) return tradeReviewCompactSymbol(symbol);
  if (cleanName.length <= 6) return cleanName;
  return String(asset?.key || cleanName).trim() || cleanName;
}

function tradeReviewAssetTileText(asset, rect) {
  const area = rect.w * rect.h;
  const shortSide = Math.min(rect.w, rect.h);
  const symbol = String(asset?.symbol || '').trim();
  const name = tradeReviewAssetName(asset);
  const cleanName = tradeReviewAssetCleanName(asset);
  const compactSymbol = tradeReviewCompactSymbol(symbol);
  const symbolIsPrimary = Boolean(symbol && name === symbol && cleanName && cleanName !== symbol);
  if (area < 120 || shortSide < 6.8) {
    return {
      meta: '',
      name: tradeReviewAssetShortLabel(asset),
    };
  }
  if (area < 420 || shortSide < 12) {
    if (shortSide < 12) {
      return {
        meta: '',
        name: tradeReviewAssetShortLabel(asset),
      };
    }
    return {
      meta: symbolIsPrimary ? '' : compactSymbol,
      name: name.length > 6 ? tradeReviewAssetShortLabel(asset) : name,
    };
  }
  return {
    meta: symbolIsPrimary ? cleanName : compactSymbol,
    name,
  };
}

function tradeReviewAssetFullLabel(asset) {
  const symbol = String(asset?.symbol || '').trim();
  const cleanName = tradeReviewAssetCleanName(asset);
  if (symbol && cleanName && cleanName !== symbol) return `${symbol} · ${cleanName}`;
  return cleanName || symbol || '--';
}

function tradeReviewAssetWeight(asset) {
  const effect = Math.abs(tradeReviewNumber(asset?.estimated_decision_effect_cny, 0));
  const weight = Math.abs(tradeReviewNumber(asset?.weight_cny, 0));
  const count = Math.max(0, tradeReviewNumber(asset?.trade_count, 0));
  if (effect > 0.01) return effect;
  if (weight > 0.01) return weight;
  return Math.max(count * 120, 1);
}

function tradeReviewAssetTone(asset) {
  const tone = String(asset?.impact_tone || '').toLowerCase();
  if (['gain', 'loss', 'flat'].includes(tone)) return tone;
  return tradeReviewHeatTone(asset?.estimated_decision_effect_cny);
}

function tradeReviewAssetTiles(payload) {
  return (payload?.asset_heatmap?.assets || [])
    .filter((asset) => asset && asset.key)
    .slice(0, 72);
}

function tradeReviewTreemapWeight(asset) {
  return Math.max(Math.pow(tradeReviewAssetWeight(asset), 0.9), 1);
}

function tradeReviewTreemapSplit(items) {
  const total = items.reduce((sum, item) => sum + item.layoutWeight, 0);
  const target = total / 2;
  let bestIndex = 1;
  let bestDiff = Number.POSITIVE_INFINITY;
  let running = 0;
  for (let index = 0; index < items.length - 1; index += 1) {
    running += items[index].layoutWeight;
    const diff = Math.abs(target - running);
    if (diff < bestDiff) {
      bestDiff = diff;
      bestIndex = index + 1;
    }
  }
  return bestIndex;
}

function tradeReviewTreemapPartition(items, rect) {
  if (!items.length) return [];
  if (items.length === 1) {
    return [{ ...items[0], rect }];
  }
  const total = items.reduce((sum, item) => sum + item.layoutWeight, 0);
  const splitIndex = tradeReviewTreemapSplit(items);
  const first = items.slice(0, splitIndex);
  const second = items.slice(splitIndex);
  const firstWeight = first.reduce((sum, item) => sum + item.layoutWeight, 0);
  const ratio = total > 0 ? firstWeight / total : 0.5;
  if (rect.w >= rect.h) {
    const firstWidth = rect.w * ratio;
    return [
      ...tradeReviewTreemapPartition(first, { x: rect.x, y: rect.y, w: firstWidth, h: rect.h }),
      ...tradeReviewTreemapPartition(second, { x: rect.x + firstWidth, y: rect.y, w: rect.w - firstWidth, h: rect.h }),
    ];
  }
  const firstHeight = rect.h * ratio;
  return [
    ...tradeReviewTreemapPartition(first, { x: rect.x, y: rect.y, w: rect.w, h: firstHeight }),
    ...tradeReviewTreemapPartition(second, { x: rect.x, y: rect.y + firstHeight, w: rect.w, h: rect.h - firstHeight }),
  ];
}

function tradeReviewTreemapLayout(assets) {
  const items = assets
    .map((asset, index) => ({ asset, index, layoutWeight: tradeReviewTreemapWeight(asset) }))
    .sort((a, b) => b.layoutWeight - a.layoutWeight || a.index - b.index);
  return tradeReviewTreemapPartition(items, { x: 0, y: 0, w: 100, h: 100 });
}

function tradeReviewAssetTileClass(rect) {
  const area = rect.w * rect.h;
  const shortSide = Math.min(rect.w, rect.h);
  if (area < 42 || shortSide < 3.9) return 'is-micro';
  if (area < 92 || shortSide < 5.8) return 'is-xs';
  if (area < 180 || shortSide < 8) return 'is-sm';
  if (area < 360) return 'is-md';
  if (area < 820) return 'is-lg';
  return 'is-xl';
}

function tradeReviewClamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function tradeReviewAssetTileStyle(rect) {
  const area = rect.w * rect.h;
  const shortSide = Math.min(rect.w, rect.h);
  const rawBase = Math.sqrt(area) * 0.72 + shortSide * 0.16;
  const base = tradeReviewClamp(Math.min(rawBase, rect.w * 0.5 + 2.5, rect.h * 0.58 + 2), 7, 18);
  const meta = tradeReviewClamp(Math.min(base * 0.56, rect.w * 0.3 + 1.5), 6.2, 10.5);
  const value = tradeReviewClamp(Math.min(base * 0.9, rect.w * 0.42 + 2), 7, 15.5);
  const pad = tradeReviewClamp(shortSide * 0.34, 1.5, 9);
  const lines = rect.h >= 15 && rect.w >= 10 ? 2 : 1;
  return [
    `--asset-x:${rect.x.toFixed(3)}%`,
    `--asset-y:${rect.y.toFixed(3)}%`,
    `--asset-w:${rect.w.toFixed(3)}%`,
    `--asset-h:${rect.h.toFixed(3)}%`,
    `--asset-name:${base.toFixed(2)}px`,
    `--asset-meta:${meta.toFixed(2)}px`,
    `--asset-value:${value.toFixed(2)}px`,
    `--asset-pad:${pad.toFixed(2)}px`,
    `--asset-lines:${lines}`,
  ].join(';');
}

function tradeReviewAssetTags(asset) {
  return (asset?.tags || [])
    .filter((tag) => tag?.key)
    .slice(0, 4)
    .map((tag) => `<span>${escapeHtml(reviewAttributionLabel(tag.key))}</span>`)
    .join('');
}

function tradeReviewTradeTone(trade) {
  const tone = String(trade?.contribution_tone || '').toLowerCase();
  if (['gain', 'loss', 'flat'].includes(tone)) return tone;
  return tradeReviewHeatTone(trade?.estimated_decision_effect_cny);
}

function tradeReviewContributionLabel(trade) {
  const label = String(trade?.contribution_label || '').trim();
  if (label) return label;
  const tone = tradeReviewTradeTone(trade);
  if (tone === 'gain') return '正贡献';
  if (tone === 'loss') return '负贡献';
  return '中性';
}

function tradeReviewTradeMoveLabel(trade) {
  const side = String(trade?.side || '').toLowerCase();
  const prefix = side === 'sell' ? '卖后' : side === 'buy' ? '买后' : '交易后';
  return `${prefix} ${formatPctPoint(trade?.current_return_pct, 1)}`;
}

function tradeReviewTradeReason(trade) {
  return trade?.contribution_reason || trade?.auto_summary || trade?.label || '暂时只有金额贡献，缺少可解释标签。';
}

function tradeReviewTradeTags(trade) {
  const seen = new Set();
  const values = [];
  const side = String(trade?.side || '').toLowerCase();
  const mainReason = side === 'sell' ? trade?.sell_reason : trade?.buy_reason;
  const suppressed = String(trade?.auto_kind || '') === 'closed_stop_loss_buy'
    ? new Set(['ignored_stop_loss', 'delayed_stop_loss'])
    : new Set();
  if (mainReason) values.push(mainReason);
  if (trade?.auto_label) values.push(trade.auto_label);
  (trade?.review_tags || []).forEach((tag) => {
    if (!suppressed.has(tag)) values.push(tag);
  });
  return values
    .map((value) => reviewAttributionLabel(value))
    .filter((label) => {
      const key = String(label || '').trim();
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 4)
    .map((label) => `<span>${escapeHtml(label)}</span>`)
    .join('');
}

function TradeReviewAssetTradeCard(trade) {
  const tone = tradeReviewTradeTone(trade);
  const tags = tradeReviewTradeTags(trade);
  return `
    <button type="button" class="tradeReviewAssetTrade ${tone}" data-trade-review="${escapeHtml(String(trade.trade_id || ''))}">
      <div class="tradeReviewAssetTradeTop">
        <span>${escapeHtml(trade.trade_date || '--')} · ${escapeHtml(sideLabel(trade.side))}</span>
        <b class="${tone}">${escapeHtml(tradeReviewContributionLabel(trade))}</b>
      </div>
      <div class="tradeReviewAssetTradeMain">
        <strong>${escapeHtml(trade.label || trade.name || trade.symbol || '--')}</strong>
        <em class="${pctTone(trade.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(trade.estimated_decision_effect_cny))}</em>
      </div>
      <p>${escapeHtml(tradeReviewTradeReason(trade))}</p>
      <div class="tradeReviewAssetTradeMeta">
        <span>${escapeHtml(tradeReviewTradeMoveLabel(trade))}</span>
        ${tags}
      </div>
    </button>
  `;
}

function TradeReviewAssetDetail(asset, selected) {
  const trades = (asset.trades || []).slice(0, 8);
  return `
    <section class="tradeReviewAssetDetail" data-asset-heat-detail="${escapeHtml(asset.key)}"${selected ? '' : ' hidden'}>
      <div class="tradeReviewAssetDetailHead">
        <div>
          <span>${escapeHtml(tradeReviewAssetMeta(asset))}</span>
          <strong>${escapeHtml(tradeReviewAssetName(asset))}</strong>
        </div>
        <em class="${pctTone(asset.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(asset.estimated_decision_effect_cny))}</em>
      </div>
      <div class="tradeReviewAssetDetailStats">
        <span>${escapeHtml(asset.trade_count || 0)} 笔</span>
        <span>买 ${escapeHtml(asset.buy_count || 0)}</span>
        <span>卖 ${escapeHtml(asset.sell_count || 0)}</span>
        <span>${escapeHtml(formatPctPoint(asset.avg_return_pct, 1))}</span>
      </div>
      ${tradeReviewAssetTags(asset) ? `<div class="tradeReviewAssetTagStrip">${tradeReviewAssetTags(asset)}</div>` : ''}
      <div class="tradeReviewAssetTradeList">
        ${trades.length ? trades.map((trade) => `
          <button type="button" class="tradeReviewAssetTrade" data-trade-review="${escapeHtml(String(trade.trade_id || ''))}">
            <span>${escapeHtml(trade.trade_date || '--')} · ${escapeHtml(sideLabel(trade.side))}</span>
            <strong>${escapeHtml(trade.label || trade.name || trade.symbol || '--')}</strong>
            <em class="${pctTone(trade.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(trade.estimated_decision_effect_cny))}</em>
          </button>
        `).join('') : '<div class="emptyState">暂无可打开的交易。</div>'}
      </div>
    </section>
  `;
}

function TradeReviewAssetAttributionDetail(asset, selected) {
  const trades = (asset.trades || []).slice(0, 8);
  const positiveCount = asset.positive_count ?? trades.filter((trade) => tradeReviewTradeTone(trade) === 'gain').length;
  const negativeCount = asset.negative_count ?? trades.filter((trade) => tradeReviewTradeTone(trade) === 'loss').length;
  return `
    <section class="tradeReviewAssetDetail tradeReviewAssetAttributionDetail" data-asset-heat-detail="${escapeHtml(asset.key)}"${selected ? '' : ' hidden'}>
      <div class="tradeReviewAssetDetailHead">
        <div>
          <span>${escapeHtml(tradeReviewAssetMeta(asset))}</span>
          <strong>${escapeHtml(tradeReviewAssetName(asset))}</strong>
        </div>
        <em class="${pctTone(asset.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(asset.estimated_decision_effect_cny))}</em>
      </div>
      <div class="tradeReviewAssetDetailStats">
        <span>${escapeHtml(asset.trade_count || 0)} 笔</span>
        <span>正 ${escapeHtml(positiveCount)}</span>
        <span>负 ${escapeHtml(negativeCount)}</span>
        <span>${escapeHtml(formatPctPoint(asset.avg_return_pct, 1))}</span>
      </div>
      ${tradeReviewAssetTags(asset) ? `<div class="tradeReviewAssetTagStrip">${tradeReviewAssetTags(asset)}</div>` : ''}
      <div class="tradeReviewAssetTradeList attribution">
        ${trades.length ? trades.map(TradeReviewAssetTradeCard).join('') : '<div class="emptyState">暂无可打开的交易。</div>'}
      </div>
    </section>
  `;
}

function tradeReviewDirectionalTrades(payload) {
  const seen = new Set();
  const heatmap = payload?.asset_heatmap || {};
  const source = Array.isArray(heatmap.directional_trades) && heatmap.directional_trades.length
    ? heatmap.directional_trades
    : (heatmap.assets || []).flatMap((asset) => (asset.trades || []).map((trade) => ({
      ...trade,
      asset_name: trade.name || asset.name || asset.symbol || trade.symbol,
      asset_symbol: trade.symbol || asset.symbol,
    })));
  return source
    .filter((trade) => {
      const key = trade.trade_id ? `id:${trade.trade_id}` : [
        trade.asset_symbol || trade.asset_name || '',
        trade.trade_date || '',
        trade.side || '',
        trade.estimated_decision_effect_cny || '',
      ].join('|');
      if (seen.has(key)) return false;
      seen.add(key);
      return trade.estimated_decision_effect_cny != null;
    });
}

function tradeReviewDirectionSort(a, b) {
  return Math.abs(tradeReviewNumber(b.estimated_decision_effect_cny, 0))
    - Math.abs(tradeReviewNumber(a.estimated_decision_effect_cny, 0));
}

function tradeReviewDirectionMatchesFilter(trade, filter) {
  const tone = tradeReviewTradeTone(trade);
  const side = String(trade?.side || '').toLowerCase();
  if (filter === 'loss') return tone === 'loss';
  if (filter === 'gain') return tone === 'gain';
  if (filter === 'buy') return side === 'buy';
  if (filter === 'sell') return side === 'sell';
  return true;
}

function tradeReviewDirectionTradeRow(trade, hidden = false) {
  const tone = tradeReviewTradeTone(trade);
  const side = String(trade?.side || '').toLowerCase();
  return `
    <button type="button"
      class="tradeReviewDirectionTrade ${tone}"
      data-trade-review="${escapeHtml(String(trade.trade_id || ''))}"
      data-direction-tone="${escapeHtml(tone)}"
      data-direction-side="${escapeHtml(side)}"${hidden ? ' hidden' : ''}>
      <div>
        <span>${escapeHtml(trade.trade_date || '--')} · ${escapeHtml(sideLabel(trade.side))}</span>
        <strong>${escapeHtml(trade.asset_name || trade.name || trade.symbol || '--')}</strong>
      </div>
      <div>
        <b>${escapeHtml(trade.label || trade.auto_label || tradeReviewContributionLabel(trade))}</b>
        <em class="${pctTone(trade.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(trade.estimated_decision_effect_cny))}</em>
      </div>
      <p>${escapeHtml(tradeReviewTradeReason(trade))}</p>
    </button>
  `;
}

function tradeReviewDirectionFilterButton(label, value, count, active = false) {
  return `
    <button type="button"
      class="${active ? 'active' : ''}"
      data-direction-filter-value="${escapeHtml(value)}"
      aria-pressed="${active ? 'true' : 'false'}">
      ${escapeHtml(label)} <span>${escapeHtml(count)}</span>
    </button>
  `;
}

function TradeReviewDirectionalTradePanel(payload) {
  const trades = tradeReviewDirectionalTrades(payload).sort(tradeReviewDirectionSort);
  const limit = 10;
  const counts = {
    all: trades.length,
    loss: trades.filter((trade) => tradeReviewDirectionMatchesFilter(trade, 'loss')).length,
    gain: trades.filter((trade) => tradeReviewDirectionMatchesFilter(trade, 'gain')).length,
    buy: trades.filter((trade) => tradeReviewDirectionMatchesFilter(trade, 'buy')).length,
    sell: trades.filter((trade) => tradeReviewDirectionMatchesFilter(trade, 'sell')).length,
  };
  const visibleCount = Math.min(limit, counts.all);
  return `
    <section class="tradeReviewDirectionalPanel" data-direction-panel data-direction-limit="${escapeHtml(limit)}">
      <div class="tradeReviewDirectionalIntro">
        <div>
          <strong>交易动作明细</strong>
          <span data-direction-count>显示 ${escapeHtml(visibleCount)} / ${escapeHtml(counts.all)} 笔</span>
        </div>
        <button type="button" class="tradeReviewDirectionFilterToggle" data-direction-filter-toggle aria-expanded="false">
          筛选
        </button>
      </div>
      <div class="tradeReviewDirectionFilters" data-direction-filter-panel hidden>
        ${tradeReviewDirectionFilterButton('全部', 'all', counts.all, true)}
        ${tradeReviewDirectionFilterButton('负向', 'loss', counts.loss)}
        ${tradeReviewDirectionFilterButton('正向', 'gain', counts.gain)}
        ${tradeReviewDirectionFilterButton('买入', 'buy', counts.buy)}
        ${tradeReviewDirectionFilterButton('卖出', 'sell', counts.sell)}
      </div>
      <div class="tradeReviewDirectionList" data-direction-list>
        ${trades.length ? trades.map((trade, index) => tradeReviewDirectionTradeRow(trade, index >= limit)).join('') : '<div class="emptyState">暂无对应交易。</div>'}
      </div>
    </section>
  `;
}

function TradeReviewAssetHeatmap(payload, workbench) {
  const assets = tradeReviewAssetTiles(payload);
  const layout = tradeReviewTreemapLayout(assets);
  const gainCount = payload?.asset_heatmap?.positive_action_count
    ?? assets.reduce((sum, asset) => sum + tradeReviewNumber(asset.positive_count, 0), 0);
  const lossCount = payload?.asset_heatmap?.negative_action_count
    ?? assets.reduce((sum, asset) => sum + tradeReviewNumber(asset.negative_count, 0), 0);
  if (!assets.length) {
    return '<section class="tradeReviewAssetHeatmapPanel"><div class="emptyState">暂无可生成热力图的交易。</div></section>';
  }
  return `
    <section class="tradeReviewAssetHeatmapPanel" data-asset-heatmap-panel>
      <div class="tradeReviewAssetHeatmapHead">
        <div>
          <span>标的热力图</span>
          <strong>${escapeHtml(assets.length)} 个标的 · ${escapeHtml(payload?.asset_heatmap?.trade_count || payload?.sample_count || 0)} 笔动作</strong>
        </div>
        <div class="tradeReviewAssetLegend">
          <span class="loss">负向动作 ${escapeHtml(lossCount)}</span>
          <span class="gain">正向动作 ${escapeHtml(gainCount)}</span>
        </div>
      </div>
      <div class="tradeReviewAssetHeatmapCanvas" data-asset-heatmap>
        ${layout.map((item) => {
          const { asset, rect } = item;
          const text = tradeReviewAssetTileText(asset, rect);
          const effectText = formatCnyEffectCompact(asset.estimated_decision_effect_cny);
          const fullLabel = `${tradeReviewAssetFullLabel(asset)} ${effectText}`;
          return `
            <button type="button"
              class="tradeReviewAssetTile ${tradeReviewAssetTone(asset)} ${tradeReviewAssetTileClass(rect)}"
              style="${tradeReviewAssetTileStyle(rect)}"
              data-asset-heat-select="${escapeHtml(asset.key)}"
              title="${escapeHtml(fullLabel)}"
              aria-pressed="false">
              <span>${escapeHtml(text.meta)}</span>
              <strong>${escapeHtml(text.name)}</strong>
              <em>${escapeHtml(effectText)}</em>
              <small>${escapeHtml(asset.trade_count || 0)} 笔 · ${escapeHtml(formatPctPoint(asset.avg_return_pct, 1))}</small>
            </button>
          `;
        }).join('')}
      </div>
      <div class="tradeReviewAssetFloatLayer" data-asset-heat-float hidden>
        <button type="button" class="tradeReviewAssetFloatScrim" data-asset-heat-close aria-label="关闭标的详情"></button>
        <div class="tradeReviewAssetFloatCard" role="dialog" aria-modal="true" aria-label="标的交易归因详情">
          <button type="button" class="tradeReviewAssetFloatClose" data-asset-heat-close aria-label="关闭">关闭</button>
          ${assets.map((asset) => TradeReviewAssetAttributionDetail(asset, false)).join('')}
        </div>
      </div>
      <details class="tradeReviewAnalysisFold tradeReviewCompactFold">
        <summary>
          <strong>动作归因</strong>
          <span>正向 / 负向动作</span>
        </summary>
        ${TradeReviewDirectionalTradePanel(payload)}
      </details>
      <p class="tradeReviewAssetHint">真实盈亏看交易页；这里看动作时点。</p>
    </section>
  `;
}

function tradeReviewPatternTiles(patterns, opts = {}) {
  const limit = opts.limit || 14;
  const rowsPerGroup = opts.rowsPerGroup || 4;
  const tiles = [];
  (patterns || []).forEach((group, groupIndex) => {
    (group.rows || []).slice(0, rowsPerGroup).forEach((row, rowIndex) => {
      const effect = tradeReviewNumber(row.estimated_decision_effect_cny, 0);
      const exposure = Math.abs(tradeReviewNumber(row.gross_exposure_cny, 0));
      const sampleCount = Math.max(0, tradeReviewNumber(row.sample_count, 0));
      const fallbackWeight = Math.max(Math.abs(effect) * 4, sampleCount * 2000, 1);
      tiles.push({
        id: `${groupIndex}-${rowIndex}`,
        groupTitle: group.title || '',
        label: reviewAttributionLabel(row.key || row.label),
        row,
        effect,
        exposure,
        sampleCount,
        weight: Math.max(exposure, fallbackWeight),
        examples: (row.examples || [])
          .filter((trade) => trade.trade_id && trade.estimated_decision_effect_cny != null)
          .slice(0, 4),
      });
    });
  });
  return tiles
    .sort((a, b) => b.weight - a.weight)
    .slice(0, limit);
}

function tradeReviewHeatTileStyle(tile, maxWeight) {
  const ratio = maxWeight > 0 ? tile.weight / maxWeight : 0;
  const grow = Math.max(1, Math.min(4.8, 1 + ratio * 3.8));
  const basis = ratio >= 0.72 ? '34%' : ratio >= 0.42 ? '26%' : ratio >= 0.2 ? '19%' : '14%';
  return `--tile-grow:${grow.toFixed(2)};--tile-basis:${basis};`;
}

function TradeReviewPatternHeatmap(patterns, opts = {}) {
  const tiles = tradeReviewPatternTiles(patterns, {
    limit: opts.heatLimit || 14,
    rowsPerGroup: opts.rowsPerGroup || 4,
  });
  const maxWeight = Math.max(...tiles.map((tile) => tile.weight), 0);
  const lossCount = tiles.filter((tile) => tile.effect < -0.01).length;
  const flatCount = tiles.filter((tile) => Math.abs(tile.effect) <= 0.01).length;
  const gainCount = tiles.filter((tile) => tile.effect > 0.01).length;
  const topLoss = tiles.filter((tile) => tile.effect < -0.01).sort((a, b) => a.effect - b.effect)[0];
  const topGain = tiles.filter((tile) => tile.effect > 0.01).sort((a, b) => b.effect - a.effect)[0];
  if (!tiles.length) {
    return '<div class="emptyState">暂无足够样本生成模式热力图。</div>';
  }
  return `
    <div class="tradeReviewHeatmap">
      <div class="tradeReviewHeatmapHead">
        <div>
          <strong>${escapeHtml(opts.title || '模式热力图')}</strong>
          <span>${escapeHtml(opts.subtitle || '面积≈交易规模，颜色=估算决策影响；标签为多选口径，不做加总。')}</span>
        </div>
        <div class="tradeReviewHeatmapLegend">
          <span class="loss">拖累 ${escapeHtml(lossCount)}</span>
          <span class="flat">中性 ${escapeHtml(flatCount)}</span>
          <span class="gain">贡献 ${escapeHtml(gainCount)}</span>
        </div>
      </div>
      <div class="tradeReviewHeatmapCanvas">
        <div class="tradeReviewHeatmapTiles">
          ${tiles.map((tile) => {
            const ratio = maxWeight > 0 ? tile.weight / maxWeight : 0;
            const sizeClass = ratio >= 0.62 ? 'is-large' : ratio >= 0.28 ? 'is-medium' : 'is-small';
            const primaryTrade = tile.examples[0];
            return `
              <article class="tradeReviewHeatTile ${tradeReviewHeatTone(tile.effect)} ${sizeClass}" style="${tradeReviewHeatTileStyle(tile, maxWeight)}">
                <div class="tradeReviewHeatTileBody">
                  <span>${escapeHtml(tile.groupTitle || '模式')}</span>
                  <strong>${escapeHtml(tile.label || '--')}</strong>
                  <em class="${pctTone(tile.effect)}">${escapeHtml(formatCnyEffect(tile.effect))}</em>
                  <div class="tradeReviewHeatTileMeta">
                    <small>样本 ${escapeHtml(tile.sampleCount)}</small>
                    <small>正向 ${escapeHtml(formatDecisionRate(tile.row.win_rate))}</small>
                  </div>
                </div>
                ${primaryTrade ? `
                  <button type="button" class="tradeReviewHeatTrade tradeReviewPatternTrade" data-trade-review="${escapeHtml(String(primaryTrade.trade_id))}">
                    <span>${escapeHtml(primaryTrade.name || primaryTrade.symbol || '--')}</span>
                    <small>${escapeHtml(primaryTrade.trade_date || '--')} · ${escapeHtml(sideLabel(primaryTrade.side))}${tile.examples.length > 1 ? ` · +${tile.examples.length - 1}` : ''}</small>
                  </button>
                ` : '<div class="tradeReviewHeatTrades muted">暂无代表交易</div>'}
              </article>
            `;
          }).join('')}
        </div>
        <aside class="tradeReviewHeatmapAside">
          <div class="tradeReviewHeatSummary neutral">
            <span>展示口径</span>
            <strong>${escapeHtml(tiles.length)} 个模式</strong>
            <small>行为标签可重叠，净影响不加总</small>
          </div>
          <div class="tradeReviewHeatFocusRows">
            ${topLoss ? `
              <div class="tradeReviewHeatFocus loss">
                <span>最大拖累</span>
                <strong>${escapeHtml(topLoss.label || '--')}</strong>
                <em>${escapeHtml(formatCnyEffect(topLoss.effect))}</em>
              </div>
            ` : ''}
            ${topGain ? `
              <div class="tradeReviewHeatFocus gain">
                <span>最大贡献</span>
                <strong>${escapeHtml(topGain.label || '--')}</strong>
                <em>${escapeHtml(formatCnyEffect(topGain.effect))}</em>
              </div>
            ` : ''}
          </div>
        </aside>
      </div>
    </div>
  `;
}

function TradeReviewKpiGrid(workbench, payload) {
  const kpis = workbench?.kpis || [
    { label: '可分析交易', value: payload?.sample_count || 0, hint: '有价格覆盖' },
    { label: '数据缺口', value: payload?.missing_price_count || 0, hint: '缺价格' },
  ];
  return `
    <div class="tradeReviewKpiGrid">
      ${kpis.map((item) => `
        <div>
          <span>${escapeHtml(item.label || '')}</span>
          <strong>${escapeHtml(item.value ?? '--')}</strong>
          <small>${escapeHtml(item.hint || '')}</small>
        </div>
      `).join('')}
    </div>
  `;
}

function TradeReviewConclusions(workbench) {
  const conclusions = workbench?.conclusions || [];
  return `
    <section class="tradeReviewDecisionPanel">
      <div class="tradeReviewSectionHead">
        <strong>核心结论</strong>
        <span>系统先给复盘判断，再看证据</span>
      </div>
      <div class="tradeReviewConclusionGrid">
        ${conclusions.length ? conclusions.slice(0, 2).map((item) => `
          <article class="tradeReviewConclusion ${workbenchTone(item.level)}">
            <span>${escapeHtml(item.metric || '结论')}</span>
            <strong>${escapeHtml(item.title || '--')}</strong>
            <p>${escapeHtml(item.detail || '')}</p>
          </article>
        `).join('') : '<div class="emptyState">暂无可读结论。</div>'}
      </div>
    </section>
  `;
}

function TradeReviewPriorityList(workbench) {
  const items = workbench?.priority_items || [];
  return `
    <section class="tradeReviewPriorityPanel">
      <div class="tradeReviewSectionHead">
        <strong>优先复盘清单</strong>
        <span>只显示最该先看的 3 笔</span>
      </div>
      <div class="tradeReviewPriorityRows">
        ${items.length ? items.slice(0, 3).map((item, idx) => {
          const auto = item.auto_review || {};
          const tone = workbenchTone(auto.level);
          return `
            <article class="tradeReviewPriorityRow ${tone}">
              <div class="tradeReviewPriorityRank">${idx + 1}</div>
              <div class="tradeReviewPriorityMain">
                <span>${escapeHtml(item.trade_date || '--')} · ${escapeHtml(sideLabel(item.side))}</span>
                <strong>${escapeHtml(item.name || item.symbol || '--')}</strong>
                <p>${escapeHtml(auto.summary || auto.label || '等待人工复盘')}</p>
              </div>
              <div class="tradeReviewPriorityMeta">
                <span class="tradeReviewLevel ${tone}">${escapeHtml(auto.label || auto.kind || '待复盘')}</span>
                <strong class="${pctTone(item.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(item.estimated_decision_effect_cny))}</strong>
                ${item.trade_id ? `<button type="button" class="miniBtn" data-trade-review="${escapeHtml(String(item.trade_id))}">补复盘</button>` : ''}
              </div>
            </article>
          `;
        }).join('') : '<div class="emptyState">暂无高优先级复盘项。</div>'}
      </div>
    </section>
  `;
}

function TradeReviewPatternPanelLegacy(workbench, opts = {}) {
  const patterns = workbench?.patterns || [];
  return `
    <section class="tradeReviewPatternPanel">
      <div class="tradeReviewPatternIntro">
        <strong>默认展示每组拖累最大的模式</strong>
        <span>按金额影响从差到好排序；点代表交易可直接打开复盘。</span>
      </div>
      <div class="tradeReviewPatternGrid">
        ${patterns.slice(0, opts.limit || patterns.length).map((group) => `
          <article class="tradeReviewPatternCard">
            <div class="tradeReviewPatternHead">
              <strong>${escapeHtml(group.title || '--')}</strong>
              <span>${escapeHtml(group.subtitle || '')}</span>
            </div>
            <div class="tradeReviewPatternRows">
              ${(group.rows || []).length ? (group.rows || []).slice(0, 3).map((row) => `
                <div class="tradeReviewPatternRow">
                  <div class="tradeReviewPatternRowMain">
                    <div>
                      <strong>${escapeHtml(reviewAttributionLabel(row.key || row.label))}</strong>
                      <small>样本 ${escapeHtml(row.sample_count || 0)} · 平均 ${escapeHtml(formatPctPoint(row.avg_return_pct))}</small>
                    </div>
                    <div>
                      <strong class="${pctTone(row.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(row.estimated_decision_effect_cny))}</strong>
                      <small>${escapeHtml(row.takeaway || '')}</small>
                    </div>
                  </div>
                  ${(row.examples || []).length ? `
                    <div class="tradeReviewPatternExamples">
                      <span>代表交易</span>
                      ${(row.examples || []).slice(0, 3).map((trade) => `
                        <button type="button" class="tradeReviewPatternTrade" data-trade-review="${escapeHtml(String(trade.trade_id || ''))}">
                          <strong>${escapeHtml(trade.name || trade.symbol || '--')}</strong>
                          <em>${escapeHtml(trade.trade_date || '--')} · ${escapeHtml(sideLabel(trade.side))}</em>
                          <small class="${pctTone(trade.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(trade.estimated_decision_effect_cny))}</small>
                        </button>
                      `).join('')}
                    </div>
                  ` : ''}
                </div>
              `).join('') : '<div class="emptyState">暂无足够样本。</div>'}
            </div>
          </article>
        `).join('')}
      </div>
    </section>
  `;
}

function TradeReviewPatternList(patterns, opts = {}) {
  const rowLimit = opts.rowLimit || 3;
  return `
    <div class="tradeReviewPatternGrid">
      ${patterns.slice(0, opts.limit || patterns.length).map((group) => `
        <article class="tradeReviewPatternCard">
          <div class="tradeReviewPatternHead">
            <strong>${escapeHtml(group.title || '--')}</strong>
            <span>${escapeHtml(group.subtitle || '')}</span>
          </div>
          <div class="tradeReviewPatternRows">
            ${(group.rows || []).length ? (group.rows || []).slice(0, rowLimit).map((row) => `
              <div class="tradeReviewPatternRow">
                <div class="tradeReviewPatternRowMain">
                  <div>
                    <strong>${escapeHtml(reviewAttributionLabel(row.key || row.label))}</strong>
                    <small>样本 ${escapeHtml(row.sample_count || 0)} · 正向 ${escapeHtml(formatDecisionRate(row.win_rate))}</small>
                  </div>
                  <div>
                    <strong class="${pctTone(row.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(row.estimated_decision_effect_cny))}</strong>
                    <small>${escapeHtml(row.takeaway || '')}</small>
                  </div>
                </div>
                ${(row.examples || []).length ? `
                  <div class="tradeReviewPatternExamples">
                    <span>代表交易</span>
                    ${(row.examples || []).slice(0, 3).map((trade) => `
                      <button type="button" class="tradeReviewPatternTrade" data-trade-review="${escapeHtml(String(trade.trade_id || ''))}">
                        <strong>${escapeHtml(trade.name || trade.symbol || '--')}</strong>
                        <em>${escapeHtml(trade.trade_date || '--')} · ${escapeHtml(sideLabel(trade.side))}</em>
                        <small class="${pctTone(trade.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(trade.estimated_decision_effect_cny))}</small>
                      </button>
                    `).join('')}
                  </div>
                ` : ''}
              </div>
            `).join('') : '<div class="emptyState">暂无足够样本。</div>'}
          </div>
        </article>
      `).join('')}
    </div>
  `;
}

function TradeReviewPatternPanel(workbench, opts = {}) {
  const focusPatterns = workbench?.patterns || [];
  const universePatterns = workbench?.pattern_universe || workbench?.pattern_distribution || focusPatterns;
  const modeUniversePatterns = tradeReviewActionPatterns(universePatterns);
  const modeFocusPatterns = tradeReviewActionPatterns(focusPatterns);
  const hasFocusPatterns = modeFocusPatterns.some((group) => (group.rows || []).length);
  return `
    <section class="tradeReviewPatternPanel">
      <div class="tradeReviewPatternIntro">
        <strong>只看三条模式主线</strong>
        <span>行为标签、买入模式、卖出模式；单笔流水和仓位生命周期回交易页。</span>
      </div>
      <div class="tradeReviewFocusCue">
        <span>复盘顺序</span>
        <strong>1 行为标签</strong>
        <strong>2 买入模式</strong>
        <strong>3 卖出模式</strong>
        <small>行为标签可重叠，金额不可相加</small>
      </div>
      <div class="tradeReviewPatternSwitch" role="tablist" aria-label="模式诊断视图">
        <button type="button" class="active" data-pattern-view-target="all" aria-selected="true">复盘主线</button>
        <button type="button" data-pattern-view-target="focus" aria-selected="false" ${hasFocusPatterns ? '' : 'disabled'}>问题优先级</button>
      </div>
      <div class="tradeReviewPatternView" data-pattern-view="all">
        ${TradeReviewPatternHeatmap(modeUniversePatterns, {
          ...opts,
          heatLimit: opts.heatLimit || 9,
          rowsPerGroup: opts.rowsPerGroup || 8,
          title: '模式归因热力图',
          subtitle: '只保留第三层模式：行为标签、买入模式、卖出模式。',
        })}
      </div>
      <div class="tradeReviewPatternView" data-pattern-view="focus" hidden>
        ${TradeReviewPatternHeatmap(modeFocusPatterns, {
          ...opts,
          heatLimit: opts.focusHeatLimit || 9,
          rowsPerGroup: opts.focusRowsPerGroup || 4,
          title: '问题优先级热力图',
          subtitle: '只看行为、买入、卖出三类中最该先复盘的拖累项。',
        })}
      </div>
      <details class="tradeReviewPatternListFold">
        <summary>
          <strong>查看列表明细</strong>
          <span>只展开三类模式和可归因代表交易</span>
        </summary>
        ${TradeReviewPatternList(modeUniversePatterns, { ...opts, rowLimit: opts.rowLimit || 5 })}
      </details>
    </section>
  `;
}

function TradeReviewDataGaps(workbench, payload, opts = {}) {
  const gaps = workbench?.data_gaps || [];
  const note = payload?.data_quality?.note || workbench?.method_note || '';
  return `
    <section class="tradeReviewDataPanel">
      <div class="tradeReviewDataGrid">
        ${gaps.length ? gaps.map((gap) => `
          <div class="tradeReviewDataGap">
            <span>${escapeHtml(gap.label || gap.kind || '数据缺口')}</span>
            <strong>${escapeHtml(gap.count ?? 0)}</strong>
            <p>${escapeHtml(gap.action || '')}</p>
          </div>
        `).join('') : '<div class="tradeReviewDataGap good"><span>价格覆盖</span><strong>OK</strong><p>当前样本没有阻塞性的价格缺口。</p></div>'}
      </div>
      ${opts.note === false ? '' : `<p class="tradeReviewMethodNote">${escapeHtml(note)}</p>`}
    </section>
  `;
}

function TradeReviewDetailPanels(workbench, payload) {
  return `
    <details class="tradeReviewAnalysisFold">
      <summary>
        <strong>展开模式诊断和数据质量</strong>
        <span>买入/卖出模式、行为标签、数据缺口</span>
      </summary>
      ${TradeReviewPatternPanel(workbench)}
      ${TradeReviewDataGaps(workbench, payload)}
    </details>
  `;
}

function TradeReviewWorkbenchLegacy(payload) {
  const workbench = payload?.workbench || {};
  return `
    <div class="tradeReviewWorkbench tradeReviewModeOnly">
      <section class="tradeReviewModeAttribution">
        <header class="tradeReviewModeAttributionHead">
          <div>
            <span>交易动作归因</span>
            <strong>只看哪些交易动作在贡献或拖累</strong>
          </div>
          <small>单笔复盘、交易流水和卖出生命周期回到交易页处理。</small>
        </header>
        ${TradeReviewPatternPanel(workbench)}
        <p class="tradeReviewModeNote">金额为交易后价格变动影响估算，不等同已实现盈亏；正向率按估算决策效果是否为正计算。行为标签是多标签，一笔交易可进入多个标签，不能横向加总。</p>
      </section>
    </div>
  `;
}

function TradeReviewWorkbench(payload) {
  const workbench = payload?.workbench || {};
  return `
    <div class="tradeReviewWorkbench tradeReviewModeOnly">
      <section class="tradeReviewModeAttribution">
        <header class="tradeReviewModeAttributionHead">
          <div>
            <span>交易动作归因</span>
            <strong>只看哪些交易动作在贡献或拖累</strong>
          </div>
          <small>标的实际盈亏热力图已移到收益归因。</small>
        </header>
        ${TradeReviewDirectionalTradePanel(payload)}
        ${TradeReviewDetailPanels(workbench, payload)}
      </section>
    </div>
  `;
}

function TradeAttributionPanelV2(payload) {
  if (!payload) {
    return `
      <div id="tradeAttributionPanel" class="tradeAttributionPanel">
        <div class="muted">正在计算复盘概览...</div>
      </div>
    `;
  }
  return `
    <div id="tradeAttributionPanel" class="tradeAttributionPanel tradeAttributionPanelV2 tradeReviewWorkbenchPanel">
      ${TradeReviewWorkbench(payload)}
    </div>
  `;
}

function renderTradeAttribution(payload) {
  const panel = $('tradeAttributionPanel');
  if (!panel) return;
  panel.outerHTML = TradeAttributionPanelV2(payload);
}

function renderReviewChoiceOptions(key, selectedValue, inputName, opts = {}) {
  const selected = String(selectedValue || '');
  const items = reviewOptionList(key)
    .filter((item) => !opts.excludeValues?.includes?.(item.value));
  return `
    ${items.map((item) => {
      const checked = item.value === selected;
      return `
        <label class="tradeReviewTag ${checked ? 'checked' : ''}">
          <input type="radio" name="${escapeHtml(inputName)}" value="${escapeHtml(item.value)}"${checked ? ' checked' : ''}>
          <span>${escapeHtml(item.label)}</span>
        </label>
      `;
    }).join('')}
  `;
}

function renderStaticChoiceOptions(items, selectedValue, inputName) {
  const selected = String(selectedValue || '');
  return (items || []).map((item) => {
    const checked = item.value === selected;
    return `
      <label class="tradeReviewTag ${checked ? 'checked' : ''}">
        <input type="radio" name="${escapeHtml(inputName)}" value="${escapeHtml(item.value)}"${checked ? ' checked' : ''}>
        <span>${escapeHtml(item.label)}</span>
      </label>
    `;
  }).join('');
}

function renderOptionChoices(key, selectedValue) {
  const selected = String(selectedValue || '');
  return [
    '<option value="">不选择</option>',
    ...reviewOptionList(key).map((item) => option(item.value, item.label, item.value === selected)),
  ].join('');
}

function renderTransitionTriggerOptions(selectedTriggers) {
  const selected = new Set(Array.isArray(selectedTriggers) ? selectedTriggers : []);
  return reviewOptionList('transition_triggers').map((item) => `
    <label class="tradeReviewTag ${selected.has(item.value) ? 'checked' : ''}">
      <input type="checkbox" name="identityTransitionTrigger" value="${escapeHtml(item.value)}"${selected.has(item.value) ? ' checked' : ''}>
      <span>${escapeHtml(item.label)}</span>
    </label>
  `).join('');
}

function reviewTagDisplayLabel(item) {
  return REVIEW_TAG_LABEL_OVERRIDES[item?.value] || item?.label || item?.value || '';
}

function renderReviewTagGroup(label, items, selected) {
  if (!items.length) return '';
  return `
    <div class="tradeReviewTagGroup">
      <div class="tradeReviewGroupTitle">${escapeHtml(label)}</div>
      <div class="tradeReviewTagGrid">
        ${items.map((item) => `
          <label class="tradeReviewTag ${selected.has(item.value) ? 'checked' : ''}">
            <input type="checkbox" name="reviewTag" data-tag-group="${escapeHtml(item.group || 'post_review')}" value="${escapeHtml(item.value)}"${selected.has(item.value) ? ' checked' : ''}>
            <span>${escapeHtml(reviewTagDisplayLabel(item))}</span>
          </label>
        `).join('')}
      </div>
    </div>
  `;
}

function renderReviewTagOptions(selectedTags) {
  const selected = new Set(Array.isArray(selectedTags) ? selectedTags : []);
  const grouped = reviewOptionList('review_tags').reduce((acc, item) => {
    const group = item.group || 'post_review';
    if (!acc[group]) acc[group] = [];
    acc[group].push(item);
    return acc;
  }, {});
  const common = Object.entries(REVIEW_TAG_GROUP_LABELS).map(([group, label]) => {
    const items = (grouped[group] || []).filter((item) => COMMON_REVIEW_TAG_VALUES.has(item.value));
    return renderReviewTagGroup(label, items, selected);
  }).join('');
  const more = Object.entries(REVIEW_TAG_GROUP_LABELS).map(([group, label]) => {
    const items = (grouped[group] || []).filter((item) => !COMMON_REVIEW_TAG_VALUES.has(item.value));
    return renderReviewTagGroup(label, items, selected);
  }).join('');
  return `
    ${common}
    ${more.trim() ? `
      <details class="tradeReviewMoreTags">
        <summary>更多标签</summary>
        ${more}
      </details>
    ` : ''}
  `;
}

function findTradeRow(tradeId) {
  return state.rows.find((row) => isReviewableTrade(row) && String(row.native_id) === String(tradeId));
}

function tradeRowFromAttributionPayload(payload) {
  const trade = payload?.trade || {};
  const tradeId = trade.trade_id || payload?.trade_id;
  if (!tradeId) return null;
  const side = payload?.side || (() => {
    const tradeType = String(trade.trade_type || '').toLowerCase();
    if (['buy', 'fund_subscription'].includes(tradeType)) return 'buy';
    if (['sell', 'fund_redemption'].includes(tradeType)) return 'sell';
    return String(trade.side || tradeType || '').toLowerCase();
  })();
  const fees = Number(trade.fee_amount || 0) + Number(trade.tax_amount || 0);
  return {
    row_id: `fact-${tradeId}`,
    native_id: tradeId,
    source: 'fact',
    account_code: trade.account_code || '',
    account_name: trade.account_name || accountLabel(trade.account_code),
    trade_date: trade.trade_date || '',
    trade_time: trade.trade_time || trade.trade_date || '',
    major_category: trade.asset_major_category || '',
    minor_category: trade.asset_minor_category || '',
    symbol: trade.symbol || '',
    name: trade.name || trade.symbol || '',
    side: trade.side || side,
    side_normalized: side,
    quantity: trade.quantity,
    price: trade.price,
    gross_amount: Math.abs(Number(trade.gross_amount ?? trade.net_cash ?? 0)),
    fees: Math.abs(fees),
    currency: trade.currency || '',
    status: trade.review_status || 'auto',
    notes: trade.notes || '',
    buy_reason: trade.buy_reason || '',
    sell_reason: trade.sell_reason || '',
    review_tags: trade.review_tags || [],
    trade_intent: trade.trade_intent || '',
    trade_plan: trade.trade_plan || '',
    review_note: trade.review_note || '',
    review_due_date: trade.review_due_date || '',
    initial_position_identity: trade.initial_position_identity || '',
    current_position_identity: trade.current_position_identity || '',
    identity_confidence: trade.identity_confidence || '',
    intended_holding_period: trade.intended_holding_period || '',
    buy_logic: trade.buy_logic || '',
    add_condition: trade.add_condition || '',
    reduce_condition: trade.reduce_condition || '',
    sell_condition: trade.sell_condition || '',
    max_acceptable_loss_pct: trade.max_acceptable_loss_pct || '',
    max_position_pct: trade.max_position_pct || '',
    next_review_date: trade.next_review_date || '',
    sell_rule_type: trade.sell_rule_type || '',
  };
}

async function openTradeReviewById(tradeId) {
  const existing = findTradeRow(tradeId);
  if (existing) {
    openTradeReviewSheet(existing);
    return;
  }
  const payload = await jget(`/api/investment-transactions/${encodeURIComponent(tradeId)}/attribution`, { cache: false });
  const row = tradeRowFromAttributionPayload(payload);
  if (!row) throw new Error(`trade not found: ${tradeId}`);
  openTradeReviewSheet(row);
}

function reviewTextValue(value) {
  return escapeHtml(String(value || ''));
}

function horizonMetric(metrics, key) {
  return metrics?.horizons?.[key]?.return_pct;
}

function TradeAutoAttributionPanel(payload) {
  const metrics = payload?.event_metrics || {};
  const judgement = (payload?.judgements || [])[0] || {};
  if (!payload) {
    return `
      <section id="tradeAutoAttribution" class="tradeAutoAttribution">
        <div class="muted">正在读取交易后表现...</div>
      </section>
    `;
  }
  if (metrics.coverage !== 'ok') {
    return `
      <section id="tradeAutoAttribution" class="tradeAutoAttribution mutedPanel">
        <strong>${escapeHtml(judgement.label || '数据不足')}</strong>
        <p>${escapeHtml((metrics.warnings || payload.data_quality?.warnings || ['暂无足够价格数据。'])[0])}</p>
      </section>
    `;
  }
  return `
    <section id="tradeAutoAttribution" class="tradeAutoAttribution">
      <div class="tradeAutoHead">
        <div>
          <span>自动表现</span>
          <strong>${escapeHtml(judgement.label || '观察中')}</strong>
        </div>
        <em class="${pctTone(metrics.current_return_pct)}">${escapeHtml(formatPctPoint(metrics.current_return_pct))}</em>
      </div>
      <div class="tradeAutoMetricGrid">
        <div><span>T+1</span><strong class="${pctTone(horizonMetric(metrics, 't1'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't1')))}</strong></div>
        <div><span>T+5</span><strong class="${pctTone(horizonMetric(metrics, 't5'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't5')))}</strong></div>
        <div><span>T+20</span><strong class="${pctTone(horizonMetric(metrics, 't20'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't20')))}</strong></div>
        <div><span>最大浮亏</span><strong class="${pctTone(metrics.max_adverse_excursion_pct)}">${escapeHtml(formatPctPoint(metrics.max_adverse_excursion_pct))}</strong></div>
      </div>
      <p>${escapeHtml(judgement.message || payload.data_quality?.note || '事件级近似，未做精确 lot matching。')}</p>
    </section>
  `;
}

function TradeAutoAttributionPanelV2(payload) {
  const metrics = payload?.event_metrics || {};
  const judgement = (payload?.judgements || [])[0] || {};
  const auto = payload?.auto_review || {};
  const tone = autoLevelTone(auto.level || judgement.level);
  if (!payload) {
    return `
      <section id="tradeAutoAttribution" class="tradeAutoAttribution">
        <div class="muted">正在读取系统初判...</div>
      </section>
    `;
  }
  const evidence = Array.isArray(auto.evidence) ? auto.evidence : [];
  const manualGaps = Array.isArray(auto.manual_gaps) ? auto.manual_gaps : [];
  if (metrics.coverage !== 'ok') {
    return `
      <section id="tradeAutoAttribution" class="tradeAutoAttribution mutedPanel tradeAutoAttribution-${tone}">
        <div class="tradeAutoHead">
          <div>
            <span>系统初判</span>
            <strong>${escapeHtml(auto.label || judgement.label || '数据不足')}</strong>
          </div>
          <em>${escapeHtml(autoConfidenceLabel(auto.confidence))}</em>
        </div>
        <p>${escapeHtml(auto.summary || (metrics.warnings || payload.data_quality?.warnings || ['暂无足够价格数据。'])[0])}</p>
        ${manualGaps.length ? `
          <div class="tradeAutoManualGaps">
            <span>待补</span>
            <strong>${escapeHtml(manualGaps.join(' / '))}</strong>
          </div>
        ` : ''}
        ${autoSuggestedTagButtons(auto.suggested_tags)}
      </section>
    `;
  }
  return `
    <section id="tradeAutoAttribution" class="tradeAutoAttribution tradeAutoAttribution-${tone}">
      <div class="tradeAutoHead">
        <div>
          <span>系统初判 · ${escapeHtml(autoConfidenceLabel(auto.confidence))}</span>
          <strong>${escapeHtml(auto.label || judgement.label || '观察中')}</strong>
        </div>
        <em class="${pctTone(metrics.current_return_pct)}">${escapeHtml(formatPctPoint(metrics.current_return_pct))}</em>
      </div>
      <p>${escapeHtml(auto.summary || judgement.message || payload.data_quality?.note || '事件级近似，未做精确 lot matching。')}</p>
      <div class="tradeAutoMetricGrid">
        <div><span>T+1</span><strong class="${pctTone(horizonMetric(metrics, 't1'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't1')))}</strong></div>
        <div><span>T+5</span><strong class="${pctTone(horizonMetric(metrics, 't5'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't5')))}</strong></div>
        <div><span>T+20</span><strong class="${pctTone(horizonMetric(metrics, 't20'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't20')))}</strong></div>
        <div><span>最大浮亏</span><strong class="${pctTone(metrics.max_adverse_excursion_pct)}">${escapeHtml(formatPctPoint(metrics.max_adverse_excursion_pct))}</strong></div>
      </div>
      ${evidence.length ? `
        <div class="tradeAutoEvidence">
          ${evidence.map((item) => `<span>${escapeHtml(item)}</span>`).join('')}
        </div>
      ` : ''}
      ${manualGaps.length ? `
        <div class="tradeAutoManualGaps">
          <span>待补</span>
          <strong>${escapeHtml(manualGaps.join(' / '))}</strong>
        </div>
      ` : ''}
      ${autoSuggestedTagButtons(auto.suggested_tags)}
      <div class="tradeAutoAction">${escapeHtml(auto.action || '建议人工确认')}</div>
    </section>
  `;
}

function metricAvailable(value) {
  return value != null && value !== '' && formatPctPoint(value) !== '--';
}

function autoEvidenceRows(metrics) {
  const rows = [];
  if (metricAvailable(metrics.max_adverse_excursion_pct)) {
    rows.push({
      label: '最大浮亏',
      value: formatPctPoint(metrics.max_adverse_excursion_pct),
      tone: pctTone(metrics.max_adverse_excursion_pct),
    });
  }
  if (metricAvailable(metrics.max_favorable_excursion_pct)) {
    rows.push({
      label: '最大浮盈',
      value: formatPctPoint(metrics.max_favorable_excursion_pct),
      tone: pctTone(metrics.max_favorable_excursion_pct),
    });
  }
  const horizons = ['t1', 't5', 't20']
    .map((key) => [key.toUpperCase(), horizonMetric(metrics, key)])
    .filter(([, value]) => metricAvailable(value));
  if (horizons.length) {
    rows.push({
      label: '短期验证',
      value: horizons.map(([label, value]) => `${label} ${formatPctPoint(value)}`).join(' / '),
      tone: pctTone(horizons[horizons.length - 1][1]),
    });
  }
  return rows.slice(0, 3);
}

function AutoEvidenceList(metrics) {
  const rows = autoEvidenceRows(metrics);
  if (!rows.length) return '';
  return `
    <div class="tradeAutoEvidenceList">
      ${rows.map((row) => `
        <div>
          <span>${escapeHtml(row.label)}</span>
          <strong class="${escapeHtml(row.tone)}">${escapeHtml(row.value)}</strong>
        </div>
      `).join('')}
    </div>
  `;
}

function AutoMetricDetails(metrics) {
  return `
    <details class="tradeAutoDetailFold">
      <summary>查看指标细节</summary>
      <div class="tradeAutoMetricGrid">
        <div><span>T+1</span><strong class="${pctTone(horizonMetric(metrics, 't1'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't1')))}</strong></div>
        <div><span>T+5</span><strong class="${pctTone(horizonMetric(metrics, 't5'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't5')))}</strong></div>
        <div><span>T+20</span><strong class="${pctTone(horizonMetric(metrics, 't20'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't20')))}</strong></div>
        <div><span>价格点</span><strong>${escapeHtml(String(metrics.points || '--'))}</strong></div>
      </div>
    </details>
  `;
}

function TradeAutoAttributionPanelV3(payload) {
  const metrics = payload?.event_metrics || {};
  const judgement = (payload?.judgements || [])[0] || {};
  const auto = payload?.auto_review || {};
  const tone = autoLevelTone(auto.level || judgement.level);
  if (!payload) {
    return `
      <section id="tradeAutoAttribution" class="tradeAutoAttribution">
        <div class="muted">正在读取系统初判...</div>
      </section>
    `;
  }
  const manualGaps = Array.isArray(auto.manual_gaps) ? auto.manual_gaps : [];
  if (metrics.coverage !== 'ok') {
    return `
      <section id="tradeAutoAttribution" class="tradeAutoAttribution mutedPanel tradeAutoAttribution-${tone}">
        <div class="tradeAutoHead tradeAutoDecisionHead">
          <div>
            <span>系统结论 · ${escapeHtml(autoConfidenceLabel(auto.confidence))}</span>
            <strong>${escapeHtml(auto.label || judgement.label || '数据不足')}</strong>
          </div>
        </div>
        <p class="tradeAutoSummary">${escapeHtml(auto.summary || (metrics.warnings || payload.data_quality?.warnings || ['暂无足够价格数据。'])[0])}</p>
        ${manualGaps.length ? `<div class="tradeAutoManualGaps"><span>待补</span><strong>${escapeHtml(manualGaps.join(' / '))}</strong></div>` : ''}
        <div class="tradeAutoNextStep"><span>下一步</span><strong>${escapeHtml(auto.action || '先补价格或确认标的映射')}</strong></div>
        ${autoSuggestedTagButtons(auto.suggested_tags)}
      </section>
    `;
  }
  return `
    <section id="tradeAutoAttribution" class="tradeAutoAttribution tradeAutoAttribution-${tone}">
      <div class="tradeAutoHead tradeAutoDecisionHead">
        <div>
          <span>系统结论 · ${escapeHtml(autoConfidenceLabel(auto.confidence))}</span>
          <strong>${escapeHtml(auto.label || judgement.label || '观察中')}</strong>
        </div>
        <em class="${pctTone(metrics.current_return_pct)}">${escapeHtml(formatPctPoint(metrics.current_return_pct))}</em>
      </div>
      <p class="tradeAutoSummary">${escapeHtml(auto.summary || judgement.message || '价格表现尚未形成强结论。')}</p>
      ${AutoEvidenceList(metrics)}
      <div class="tradeAutoNextStep"><span>下一步</span><strong>${escapeHtml(auto.action || '人工确认即可')}</strong></div>
      ${manualGaps.length ? `<div class="tradeAutoManualGaps"><span>待补</span><strong>${escapeHtml(manualGaps.join(' / '))}</strong></div>` : ''}
      ${autoSuggestedTagButtons(auto.suggested_tags)}
      ${AutoMetricDetails(metrics)}
    </section>
  `;
}

function renderTradeAutoAttribution(payload) {
  const panel = $('tradeAutoAttribution');
  if (!panel) return;
  panel.outerHTML = TradeAutoAttributionPanelV3(payload);
  const form = $('tradeReviewForm');
  if (form) {
    const auto = payload?.auto_review || {};
    form.dataset.autoReviewLevel = auto.level || '';
    form.dataset.autoReviewNeedsHuman = auto.needs_human_review ? '1' : '0';
    form.dataset.autoReviewKind = auto.kind || '';
    updateTradeReviewCompletion(form);
  }
}

async function loadTradeAttribution(tradeId) {
  const payload = await jget(`/api/investment-transactions/${encodeURIComponent(tradeId)}/attribution`, { cache: false });
  renderTradeAutoAttribution(payload);
}

function addDaysIso(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function identityRule(identity) {
  return POSITION_IDENTITY_RULES[String(identity || '')] || {};
}

function ruleLabel(ruleType) {
  return reviewOptionLabel('sell_rule_types', ruleType) || identityRule(ruleType).sellCondition || '选择仓位身份后自动生成';
}

function setRadioValue(form, name, value) {
  const normalized = String(value || '');
  form.querySelectorAll(`input[name="${name}"]`).forEach((input) => {
    input.checked = Boolean(normalized && input.value === normalized);
    input.closest('.tradeReviewTag')?.classList.toggle('checked', Boolean(input.checked));
  });
}

function formInputValue(id) {
  const el = $(id);
  return el ? String(el.value || '').trim() : '';
}

function setGeneratedValue(id, value, opts = {}) {
  const el = $(id);
  if (!el || value == null) return;
  const current = String(el.value || '').trim();
  const generated = el.dataset.autoGenerated !== '0';
  if (opts.force || !current || generated) {
    el.value = value;
    el.dataset.autoGenerated = '1';
  }
}

function selectedIdentity(form) {
  return radioInputValue(form, 'reviewCurrentIdentity') || radioInputValue(form, 'reviewInitialIdentity') || '';
}

function isTransitionVisible(form) {
  const section = $('identityTransitionSection');
  return Boolean(section && !section.hidden);
}

function isClosedPosition(form) {
  return radioInputValue(form, 'reviewCurrentIdentity') === 'closed';
}

function formatCompletion(done, total) {
  return `${Math.min(done, total)}/${total}`;
}

function updateTradeReviewCompletion(form) {
  if (!form) return;
  const planIds = ['reviewBuyLogic', 'reviewAddCondition', 'reviewReduceCondition', 'reviewMaxLossPct', 'reviewNextReviewDate'];
  const planDone = planIds.filter((id) => formInputValue(id)).length;
  const planBadge = $('reviewPlanCompletion');
  if (planBadge) planBadge.textContent = `计划细节 ${formatCompletion(planDone, 5)}`;

  const tagCount = form.querySelectorAll('input[name="reviewTag"]:checked').length;
  const tagBadge = $('reviewTagCompletion');
  if (tagBadge) tagBadge.textContent = `复盘标签 ${Math.min(tagCount, 3)}/3`;

  const quality = tradeReviewQualityV2(form);
  const qualityBadge = $('tradeReviewQualityBadge');
  if (qualityBadge) {
    qualityBadge.textContent = quality.label;
    qualityBadge.className = `tradeReviewQualityBadge ${quality.level}`;
  }
}

function updateTransitionSection(form, opts = {}) {
  const initial = radioInputValue(form, 'reviewInitialIdentity');
  const current = radioInputValue(form, 'reviewCurrentIdentity');
  const section = $('identityTransitionSection');
  if (!section) return;
  if (opts.forceShow) section.dataset.manualShow = '1';
  const changed = Boolean(initial && current && initial !== current);
  section.hidden = !(changed || section.dataset.manualShow === '1');
  const oldText = $('transitionOldIdentityText');
  const newText = $('transitionNewIdentityText');
  const newRuleText = $('transitionNewSellRuleText');
  const newLimitText = $('transitionNewLimitText');
  const reviewDateText = $('transitionReviewDateText');
  if (oldText) oldText.textContent = reviewOptionLabel('position_identities', initial) || '--';
  if (newText) newText.textContent = reviewOptionLabel('current_position_identities', current) || '--';
  if (newRuleText) newRuleText.textContent = ruleLabel(formInputValue('reviewSellRuleType'));
  if (newLimitText) newLimitText.textContent = current === 'closed'
    ? '不适用'
    : (formInputValue('reviewMaxPositionPct') ? `${formInputValue('reviewMaxPositionPct')}%` : '--');
  if (reviewDateText) reviewDateText.textContent = current === 'closed'
    ? '不适用'
    : (formInputValue('reviewNextReviewDate') || '--');
  const highRisk = initial === 'trend' && current === 'value';
  const warning = $('trendToValueWarning');
  if (warning) warning.hidden = !highRisk;
}

function applyIdentityDefaults(form, opts = {}) {
  if (!form) return;
  const initial = radioInputValue(form, 'reviewInitialIdentity');
  const current = radioInputValue(form, 'reviewCurrentIdentity');
  if (initial && !current) setRadioValue(form, 'reviewCurrentIdentity', initial);
  const effectiveIdentity = selectedIdentity(form);
  const rule = identityRule(effectiveIdentity);
  if (!rule) return;
  setGeneratedValue('reviewSellRuleType', rule.sellRuleType || '', { force: opts.forceRule });
  setGeneratedValue('reviewMaxPositionPct', rule.maxPositionPct || '', { force: opts.forceLimit });
  setGeneratedValue('reviewSellCondition', rule.sellCondition || '', { force: opts.forceRule });
  if (rule.holdingPeriod) {
    const period = radioInputValue(form, 'reviewHoldingPeriod');
    if (!period || opts.forcePeriod) setRadioValue(form, 'reviewHoldingPeriod', rule.holdingPeriod);
  }
  if (effectiveIdentity === 'uncertain') {
    setGeneratedValue('reviewAddCondition', '重新分类前不允许继续加仓。');
    setGeneratedValue('reviewNextReviewDate', addDaysIso(14));
  } else if (effectiveIdentity === 'closed') {
    setGeneratedValue('reviewNextReviewDate', '', { force: opts.forceLimit });
  }
  const hint = $('identityRuleHint');
  if (hint) hint.textContent = rule.hint || '选择仓位身份后自动生成卖出规则和仓位上限。';
  const ruleText = $('reviewSellRuleText');
  if (ruleText) ruleText.textContent = ruleLabel(formInputValue('reviewSellRuleType'));
  const ruleDetail = $('reviewSellRuleDetail');
  if (ruleDetail) ruleDetail.textContent = formInputValue('reviewSellCondition') || rule.sellCondition || '选择仓位身份后自动生成。';
  const uncertainGuard = $('uncertainPositionGuard');
  if (uncertainGuard) uncertainGuard.hidden = effectiveIdentity !== 'uncertain';
  updateTransitionSection(form);
  updateTradeReviewCompletion(form);
}

function tradeReviewQuality(form) {
  const side = String(form?.dataset?.side || '').toLowerCase();
  const initial = radioInputValue(form, 'reviewInitialIdentity');
  const current = radioInputValue(form, 'reviewCurrentIdentity');
  const reason = side === 'sell' ? radioInputValue(form, 'reviewSellReason') : radioInputValue(form, 'reviewBuyReason');
  const tagCount = form?.querySelectorAll('input[name="reviewTag"]:checked').length || 0;
  const autoNeedsHuman = form?.dataset?.autoReviewNeedsHuman === '1';
  const autoLevel = String(form?.dataset?.autoReviewLevel || '');
  if (initial === 'trend' && current === 'value') return { label: '高风险身份漂移', level: 'danger' };
  const closed = current === 'closed';
  if (!reason || !initial || !current || (!closed && (!radioInputValue(form, 'reviewHoldingPeriod') || !formInputValue('reviewMaxPositionPct') || !formInputValue('reviewSellRuleType')))) {
    return { label: '信息不足', level: 'warn' };
  }
  if ([initial, current].includes('uncertain')) return { label: '待复盘', level: 'pending' };
  return { label: '完整', level: 'ok' };
}

function tradeReviewQualityV2(form) {
  const base = tradeReviewQuality(form);
  if (!base || base.level === 'warn' || base.level === 'danger') return base;
  const tagCount = form?.querySelectorAll('input[name="reviewTag"]:checked').length || 0;
  const autoNeedsHuman = form?.dataset?.autoReviewNeedsHuman === '1';
  const autoLevel = String(form?.dataset?.autoReviewLevel || '');
  if (autoLevel === 'danger') return { label: '高风险待确认', level: 'danger' };
  if (autoNeedsHuman && tagCount === 0) {
    return { label: '待确认归因', level: autoLevel === 'bad' ? 'danger' : 'pending' };
  }
  return base;
}

function openTradeReviewSheet(row) {
  const holding = holdingDisplay(row);
  const side = String(row?.side_normalized || '').toLowerCase();
  const showBuyReason = side === 'buy' || row?.buy_reason;
  const showSellReason = side === 'sell' || row?.sell_reason;
  const account = row.account_name || accountLabel(row.account_code);
  const initialIdentity = row.initial_position_identity || '';
  const currentIdentity = row.current_position_identity || initialIdentity || '';
  const effectiveIdentity = currentIdentity || initialIdentity;
  const rule = identityRule(effectiveIdentity);
  const holdingPeriod = row.intended_holding_period || rule.holdingPeriod || '';
  const sellRuleType = row.sell_rule_type || rule.sellRuleType || '';
  const sellCondition = row.sell_condition || rule.sellCondition || '';
  const maxPositionPct = row.max_position_pct || rule.maxPositionPct || '';
  const nextReviewDate = row.next_review_date || row.review_due_date || (effectiveIdentity === 'uncertain' ? addDaysIso(14) : '');
  const transitionVisible = Boolean(initialIdentity && currentIdentity && initialIdentity !== currentIdentity);
  const title = `交易复盘 · ${holding.name}`;
  openBottomSheet(title, `
    <form id="tradeReviewForm" class="tradeReviewForm" data-trade-id="${escapeHtml(String(row.native_id))}" data-side="${escapeHtml(side)}">
      <div class="tradeReviewContext">
        <div>
          <span>交易</span>
          <strong>${escapeHtml(sideLabel(side))} · ${escapeHtml(money(row.gross_amount, row.currency))}</strong>
        </div>
        <div>
          <span>账户</span>
          <strong>${escapeHtml(account || '--')}</strong>
        </div>
        <div>
          <span>日期</span>
          <strong>${escapeHtml(row.trade_date || '--')}</strong>
        </div>
      </div>

      <div class="tradeReviewModeHero">
        <div>
          <span>快速记录</span>
          <strong>30 秒记录交易目的，复杂判断之后再补。</strong>
        </div>
        <small>高级计划、事后标签、身份变化默认折叠。</small>
      </div>

      <div class="tradeReviewSubsection tradeReviewQuickSection">
        <div class="tradeReviewSubsectionHead">
          <strong>主原因</strong>
          <small>单选，不默认未标注；不确定时选“未确定 / 待复盘”。</small>
        </div>
        <div class="tradeReviewFieldGrid">
          <div class="tradeReviewField" ${showBuyReason ? '' : 'hidden'}>
            <span>买入原因</span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderReviewChoiceOptions('buy_reasons', row.buy_reason, 'reviewBuyReason')}</div>
          </div>
          <div class="tradeReviewField" ${showSellReason ? '' : 'hidden'}>
            <span>卖出原因</span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderReviewChoiceOptions('sell_reasons', row.sell_reason, 'reviewSellReason')}</div>
          </div>
        </div>

        <div class="tradeReviewFieldGrid">
          <div class="tradeReviewField">
            <span>初始仓位身份</span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderReviewChoiceOptions('position_identities', initialIdentity, 'reviewInitialIdentity')}</div>
          </div>
          <div class="tradeReviewField">
            <span>当前仓位身份 <button id="btnShowIdentityTransition" class="tradeReviewInlineBtn" type="button">修改身份</button></span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderReviewChoiceOptions('current_position_identities', currentIdentity, 'reviewCurrentIdentity')}</div>
          </div>
        </div>
        <div id="identityRuleHint" class="tradeReviewRuleHint">${escapeHtml(rule.hint || '选择仓位身份后自动生成卖出规则和仓位上限。')}</div>
        <div id="uncertainPositionGuard" class="tradeReviewWarning" ${effectiveIdentity === 'uncertain' ? '' : 'hidden'}>
          不确定可以，但必须小仓位、限期复盘、不能无限加仓。系统默认仓位上限 1%–2%，并要求设置复盘日期。
        </div>
        <div class="tradeReviewFieldGrid tradeReviewCompactGrid">
          <div class="tradeReviewField">
            <span>预计持有周期</span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderStaticChoiceOptions(HOLDING_PERIOD_OPTIONS, holdingPeriod, 'reviewHoldingPeriod')}</div>
          </div>
          <label class="tradeReviewField">
            <span>最大仓位上限 %</span>
            <input id="reviewMaxPositionPct" class="formInput mono" type="number" min="0" step="0.01" value="${escapeHtml(maxPositionPct)}" data-auto-generated="${row.max_position_pct ? '0' : '1'}" placeholder="按仓位身份建议" />
          </label>
          <div class="tradeReviewField tradeReviewRuleCard">
            <span>适用卖出规则</span>
            <input id="reviewSellRuleType" type="hidden" value="${escapeHtml(sellRuleType)}" data-auto-generated="${row.sell_rule_type ? '0' : '1'}" />
            <strong id="reviewSellRuleText">${escapeHtml(ruleLabel(sellRuleType))}</strong>
            <small id="reviewSellRuleDetail">${escapeHtml(sellCondition || '选择仓位身份后自动生成。')}</small>
          </div>
        </div>
        <label class="tradeReviewField">
          <span>一句备注（可选）</span>
          <textarea id="reviewNote" class="formInput tradeReviewTextarea compact" rows="2" placeholder="一句话记录背景、想法或主观判断。">${reviewTextValue(row.review_note)}</textarea>
        </label>
      </div>

      <details class="tradeReviewFold tradeReviewAutoFold" id="reviewAutoAttributionDetails" open>
        <summary>
          <strong>查看交易后表现</strong>
          <small>自动归因</small>
        </summary>
        ${TradeAutoAttributionPanelV3(null)}
      </details>

      <details class="tradeReviewFold" id="reviewPlanDetails">
        <summary>
          <strong>展开计划细节</strong>
          <small id="reviewPlanCompletion">计划细节 0/5</small>
        </summary>
        <div class="tradeReviewFieldGrid tradeReviewCompactGrid">
          <label class="tradeReviewField">
            <span>最大可接受亏损 %</span>
            <input id="reviewMaxLossPct" class="formInput mono" type="number" min="0" step="0.01" value="${escapeHtml(row.max_acceptable_loss_pct || '')}" />
          </label>
          <label class="tradeReviewField">
            <span>下次复盘日期</span>
            <input id="reviewNextReviewDate" class="formInput" type="date" value="${escapeHtml(nextReviewDate)}" data-auto-generated="${(row.next_review_date || row.review_due_date) ? '0' : '1'}" />
          </label>
          <label class="tradeReviewField">
            <span>身份置信度</span>
            <select id="reviewIdentityConfidence" class="formInput">${renderOptionChoices('identity_confidences', row.identity_confidence)}</select>
          </label>
        </div>
        <label class="tradeReviewField">
          <span>买入逻辑 / 不确定原因</span>
          <textarea id="reviewBuyLogic" class="formInput tradeReviewTextarea" rows="2" placeholder="记录为什么买、为什么不确定、需要补充什么研究。">${reviewTextValue(row.buy_logic)}</textarea>
        </label>
        <label class="tradeReviewField">
          <span>加仓条件</span>
          <textarea id="reviewAddCondition" class="formInput tradeReviewTextarea" rows="2" placeholder="没有重新分类前，试验仓/未确定仓不应继续加仓。">${reviewTextValue(row.add_condition)}</textarea>
        </label>
        <label class="tradeReviewField">
          <span>减仓条件</span>
          <textarea id="reviewReduceCondition" class="formInput tradeReviewTextarea" rows="2">${reviewTextValue(row.reduce_condition)}</textarea>
        </label>
        <label class="tradeReviewField">
          <span>卖出 / 证伪条件</span>
          <textarea id="reviewSellCondition" class="formInput tradeReviewTextarea" rows="2" data-auto-generated="${row.sell_condition ? '0' : '1'}" placeholder="趋势破坏、逻辑证伪、事件失败、目标比例偏离等。">${reviewTextValue(sellCondition)}</textarea>
        </label>
      </details>

      <details class="tradeReviewFold" id="reviewTagDetails">
        <summary>
          <strong>填写事后复盘</strong>
          <small id="reviewTagCompletion">复盘标签 0/3</small>
        </summary>
        <div class="tradeReviewField">
          <span>复盘标签</span>
          <div class="tradeReviewTagGrid">${renderReviewTagOptions(row.review_tags)}</div>
        </div>
      </details>

      <div id="identityTransitionSection" class="tradeReviewSubsection tradeReviewTransitionSection" ${transitionVisible ? '' : 'hidden'}>
        <div class="tradeReviewSubsectionHead">
          <strong>身份变化留痕</strong>
          <small>仅当身份变化或主动修改身份时填写。</small>
        </div>
        <div class="tradeReviewFieldGrid tradeReviewCompactGrid">
          <div class="tradeReviewReadonlyField">
            <span>原身份</span>
            <strong id="transitionOldIdentityText">${escapeHtml(reviewOptionLabel('position_identities', initialIdentity) || '--')}</strong>
          </div>
          <div class="tradeReviewReadonlyField">
            <span>新身份</span>
            <strong id="transitionNewIdentityText">${escapeHtml(reviewOptionLabel('current_position_identities', currentIdentity) || '--')}</strong>
          </div>
          <div class="tradeReviewReadonlyField">
            <span>新卖出规则</span>
            <strong id="transitionNewSellRuleText">${escapeHtml(ruleLabel(sellRuleType))}</strong>
          </div>
          <div class="tradeReviewReadonlyField">
            <span>新仓位上限 / 复盘日</span>
            <strong><em id="transitionNewLimitText">${escapeHtml(currentIdentity === 'closed' ? '不适用' : (maxPositionPct ? `${maxPositionPct}%` : '--'))}</em> · <em id="transitionReviewDateText">${escapeHtml(currentIdentity === 'closed' ? '不适用' : (nextReviewDate || '--'))}</em></strong>
          </div>
        </div>
        <div id="trendToValueWarning" class="tradeReviewWarning danger" ${initialIdentity === 'trend' && currentIdentity === 'value' ? '' : 'hidden'}>
          这是高风险身份变化。请确认不是因为亏损后不愿止损；建议补充基本面证据、估值区间、新卖出条件和新仓位上限，也可以先保存后再完善。
        </div>
        <label class="tradeReviewField">
          <span>变化类型</span>
          <select id="reviewTransitionType" class="formInput">${renderOptionChoices('transition_types', '')}</select>
        </label>
        <div class="tradeReviewField">
          <span>触发原因</span>
          <div class="tradeReviewTagGrid">${renderTransitionTriggerOptions([])}</div>
        </div>
        <label class="tradeReviewField">
          <span>变更证据</span>
          <textarea id="reviewTransitionEvidence" class="formInput tradeReviewTextarea" rows="2" placeholder="补充研究、新卖出规则、新仓位上限、估值区间或数据修正说明。"></textarea>
        </label>
      </div>

      <div id="tradeReviewMsg" class="tradeReviewMsg muted"></div>
      <div class="tradeReviewActionBar">
        <span id="tradeReviewQualityBadge" class="tradeReviewQualityBadge warn">信息不足</span>
        <button type="submit" class="btn primary tradeReviewSaveBtn">保存复盘</button>
      </div>
    </form>
  `);
  applyIdentityDefaults($('tradeReviewForm'));
  loadTradeAttribution(row.native_id).catch(() => renderTradeAutoAttribution({
    event_metrics: { coverage: 'missing', warnings: ['自动表现暂时不可用。'] },
    judgements: [{ label: '不可用', message: '自动归因接口暂时不可用。', level: 'muted' }],
  }));
}

function optionalInputValue(id) {
  const value = $(id)?.value?.trim() || '';
  return value || null;
}

function radioInputValue(form, name) {
  const value = form.querySelector(`input[name="${name}"]:checked`)?.value?.trim() || '';
  return value || null;
}

function selectedCheckboxValues(form, name) {
  return [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
}

function validateTradeReviewForm(form) {
  const side = String(form?.dataset?.side || '').toLowerCase();
  const initialIdentity = radioInputValue(form, 'reviewInitialIdentity');
  const currentIdentity = radioInputValue(form, 'reviewCurrentIdentity');
  const mainReason = side === 'sell' ? radioInputValue(form, 'reviewSellReason') : radioInputValue(form, 'reviewBuyReason');
  const reviewTagCount = form.querySelectorAll('input[name="reviewTag"]:checked').length;
  if (reviewTagCount > 3) return '复盘标签最多选择 3 个。';
  if (['buy', 'sell'].includes(side) && !mainReason) {
    return '请选择买入 / 卖出主原因；新交易不要默认留空。';
  }
  if (side === 'buy' && (!initialIdentity || !currentIdentity)) {
    return '买入交易必须补充初始仓位身份和当前仓位身份；不确定时请选择“未确定 / 待判断”。';
  }
  const closedPosition = isClosedPosition(form);
  if (!closedPosition && [initialIdentity, currentIdentity].includes('uncertain')) {
    const hasLimit = readOptionalNumber('reviewMaxPositionPct') != null;
    const hasReviewDate = Boolean(optionalInputValue('reviewNextReviewDate'));
    const hasSellRule = Boolean(optionalInputValue('reviewSellCondition'));
    const limit = readOptionalNumber('reviewMaxPositionPct');
    if (!hasLimit || !hasReviewDate || !hasSellRule) {
      return '未确定 / 待判断仓必须有最大仓位上限、下次复盘日期和临时卖出规则。';
    }
    if (limit != null && limit > 2) {
      return '未确定 / 待判断仓的最大仓位上限应控制在总资产 1%–2%。';
    }
  }
  if (isTransitionVisible(form)) {
    if (!radioInputValue(form, 'reviewInitialIdentity') || !radioInputValue(form, 'reviewCurrentIdentity')) {
      return '身份变化必须有原身份和新身份。';
    }
  }
  return '';
}

async function saveTradeReview(form) {
  const tradeId = form?.dataset?.tradeId;
  if (!tradeId) return;
  const msg = $('tradeReviewMsg');
  const validationError = validateTradeReviewForm(form);
  if (validationError) {
    if (msg) msg.textContent = validationError;
    return;
  }
  const selectedTags = selectedCheckboxValues(form, 'reviewTag');
  const transitionTriggers = selectedCheckboxValues(form, 'identityTransitionTrigger');
  const payload = {
    buy_reason: radioInputValue(form, 'reviewBuyReason'),
    sell_reason: radioInputValue(form, 'reviewSellReason'),
    review_note: optionalInputValue('reviewNote'),
    review_tags: selectedTags,
    initial_position_identity: radioInputValue(form, 'reviewInitialIdentity'),
    current_position_identity: radioInputValue(form, 'reviewCurrentIdentity'),
    identity_confidence: optionalInputValue('reviewIdentityConfidence'),
    intended_holding_period: radioInputValue(form, 'reviewHoldingPeriod'),
    buy_logic: optionalInputValue('reviewBuyLogic'),
    add_condition: optionalInputValue('reviewAddCondition'),
    reduce_condition: optionalInputValue('reviewReduceCondition'),
    sell_condition: optionalInputValue('reviewSellCondition'),
    max_acceptable_loss_pct: readOptionalNumber('reviewMaxLossPct'),
    max_position_pct: readOptionalNumber('reviewMaxPositionPct'),
    next_review_date: optionalInputValue('reviewNextReviewDate'),
    sell_rule_type: optionalInputValue('reviewSellRuleType'),
    identity_transition_type: optionalInputValue('reviewTransitionType'),
    identity_transition_triggers: transitionTriggers,
    identity_transition_evidence: optionalInputValue('reviewTransitionEvidence'),
  };
  if (msg) msg.textContent = '正在保存...';
  await jpost(`/api/investment-transactions/${encodeURIComponent(tradeId)}/review`, payload);
  if (msg) msg.textContent = '已保存';
  closeBottomSheet();
  await loadOptions();
  await loadLedger();
}

async function loadOptions() {
  const data = await jget('/api/investment-transactions/options');
  state.accounts = data.accounts || [];
  state.categories = data.categories || [];
  state.sources = data.sources || [];
  state.assets = data.assets || [];
  state.reviewOptions = data.review_options || {};
  fillSelects();
}

async function loadLedger() {
  clearError();
  const data = await jget(`/api/investment-transactions?${buildQuery()}`);
  state.rows = data.rows || [];
  state.summary = data.summary || {};
  fillTradeNameOptions(state.rows);
  renderSummary(state.summary);
  renderTransactionSummary(state.summary, state.rows);
}

function requestAttributionSummaryLoad() {
  return loadAttributionSummary().catch(() => {
    renderTradeAttribution({
      sample_count: 0,
      missing_price_count: 0,
      groups: { buy_reason: [], sell_reason: [] },
      data_quality: { note: '自动归因暂时不可用。' },
    });
  });
}

async function loadAttributionSummary() {
  const params = new URLSearchParams();
  const filters = state.filters || defaultFilterState();
  const start = filters.start || '';
  const end = filters.end || '';
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  params.set('limit', '300');
  const payload = await jget(`/api/investment-transactions/attribution/summary?${params.toString()}`, { cache: false });
  state.attribution = payload;
  state.campaigns = null;
  renderTradeAttribution(payload);
  await loadCampaignSummary();
  renderTradeAttribution(state.attribution);
}

async function loadCampaignSummary() {
  const params = new URLSearchParams();
  const filters = state.filters || defaultFilterState();
  const start = filters.start || '';
  const end = filters.end || '';
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  if (filters.assetKey) params.set('asset_key', filters.assetKey);
  params.set('limit', '5');
  try {
    state.campaigns = await jget(`/api/investment-transactions/campaigns?${params.toString()}`, { cache: false });
  } catch (err) {
    state.campaigns = {
      summary: { total_campaigns: 0, status_counts: {} },
      campaigns: [],
      data_quality: { note: err?.message || '仓位周期暂时不可用。' },
    };
  }
}

async function createTrade() {
  clearError();
  $('formMsg').textContent = '';
  const selectedAsset = resolveManualAsset($('tradeName').value);
  const amount = readOptionalNumber('tradeAmount');
  const quantity = readOptionalNumber('tradeQuantity');
  const payload = {
    trade_date: $('tradeDate').value,
    account_code: $('tradeAccount').value,
    major_category: selectedAsset.major_category || null,
    minor_category: selectedAsset.minor_category || null,
    symbol: selectedAsset.symbol || null,
    name: $('tradeName').value.trim(),
    side: $('tradeSide').value,
    quantity,
    price: amount != null && quantity ? amount / quantity : null,
    gross_amount: amount,
    fees: readOptionalNumber('tradeFees') || 0,
    currency: $('tradeCurrency').value,
    status: 'recorded',
    notes: null,
  };
  if (!payload.trade_date || !payload.account_code || !payload.name) {
    $('formMsg').textContent = '平台、交易日期、标的名称为必填';
    return;
  }
  if (payload.gross_amount == null) {
    $('formMsg').textContent = '需要填写金额';
    return;
  }
  await jpost('/api/investment-transactions', payload);
  $('formMsg').textContent = '已保存';
  state.showAll = false;
  ['tradeName', 'tradeQuantity', 'tradeAmount', 'tradeFees'].forEach((id) => {
    $(id).value = id === 'tradeFees' ? '0' : '';
  });
  await loadLedger();
}

function closeTradeReviewAssetFloat(panel) {
  const root = panel || document.querySelector('[data-asset-heatmap-panel]');
  if (!root) return;
  root.querySelector('[data-asset-heat-float]')?.setAttribute('hidden', '');
  root.querySelectorAll('[data-asset-heat-select]').forEach((button) => {
    button.classList.remove('active');
    button.setAttribute('aria-pressed', 'false');
  });
  root.querySelectorAll('[data-asset-heat-detail]').forEach((detail) => {
    detail.hidden = true;
  });
  document.body.classList.remove('trade-review-asset-open');
}

function applyTradeReviewDirectionFilter(panel, filter = 'all') {
  if (!panel) return;
  const limit = Math.max(1, Number(panel.dataset.directionLimit || 10));
  const rows = [...panel.querySelectorAll('.tradeReviewDirectionTrade')];
  let matched = 0;
  let shown = 0;

  rows.forEach((row) => {
    const tone = row.dataset.directionTone || '';
    const side = row.dataset.directionSide || '';
    const match = filter === 'all' || tone === filter || side === filter;
    if (match) matched += 1;
    const visible = match && shown < limit;
    if (visible) shown += 1;
    row.hidden = !visible;
  });

  const countLabel = panel.querySelector('[data-direction-count]');
  if (countLabel) countLabel.textContent = `显示 ${shown} / ${matched} 笔`;
  panel.querySelectorAll('[data-direction-filter-value]').forEach((button) => {
    const active = button.dataset.directionFilterValue === filter;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function bindEvents() {
  $('btnReload').addEventListener('click', loadLedger);
  $('btnReset').addEventListener('click', async () => {
    setDefaultFilters();
    await loadLedger();
  });
  $('btnCreateTrade').addEventListener('click', createTrade);
  window.addEventListener('pams:load-trade-attribution', () => {
    requestAttributionSummaryLoad();
  });
  document.addEventListener('click', async (event) => {
    const autoTagButton = event.target?.closest?.('[data-auto-tag]');
    if (autoTagButton) {
      event.preventDefault();
      const form = autoTagButton.closest('#tradeReviewForm');
      const tag = autoTagButton.dataset.autoTag;
      const input = [...(form?.querySelectorAll('input[name="reviewTag"]') || [])].find((item) => item.value === tag);
      if (!form || !input) return;
      const checkedTags = form.querySelectorAll('input[name="reviewTag"]:checked').length;
      if (!input.checked && checkedTags >= 3) {
        if ($('tradeReviewMsg')) $('tradeReviewMsg').textContent = '复盘标签最多选择 3 个。';
        return;
      }
      input.checked = true;
      input.closest('.tradeReviewTag')?.classList.add('checked');
      autoTagButton.classList.add('accepted');
      if ($('tradeReviewMsg')) $('tradeReviewMsg').textContent = '已接受系统建议标签。';
      updateTradeReviewCompletion(form);
      return;
    }
    if (event.target?.id === 'btnToggleAllTrades') {
      state.showAll = !state.showAll;
      renderTransactionSummary(state.summary, state.rows);
      return;
    }
    const patternViewButton = event.target?.closest?.('[data-pattern-view-target]');
    if (patternViewButton) {
      event.preventDefault();
      const panel = patternViewButton.closest('.tradeReviewPatternPanel');
      const target = patternViewButton.dataset.patternViewTarget;
      if (!panel || !target) return;
      panel.querySelectorAll('[data-pattern-view-target]').forEach((button) => {
        const active = button.dataset.patternViewTarget === target;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      panel.querySelectorAll('[data-pattern-view]').forEach((view) => {
        view.hidden = view.dataset.patternView !== target;
      });
      return;
    }
    const directionFilterToggle = event.target?.closest?.('[data-direction-filter-toggle]');
    if (directionFilterToggle) {
      event.preventDefault();
      const panel = directionFilterToggle.closest('[data-direction-panel]');
      const filters = panel?.querySelector('[data-direction-filter-panel]');
      if (!filters) return;
      filters.hidden = !filters.hidden;
      directionFilterToggle.setAttribute('aria-expanded', filters.hidden ? 'false' : 'true');
      return;
    }
    const directionFilterButton = event.target?.closest?.('[data-direction-filter-value]');
    if (directionFilterButton) {
      event.preventDefault();
      const panel = directionFilterButton.closest('[data-direction-panel]');
      applyTradeReviewDirectionFilter(panel, directionFilterButton.dataset.directionFilterValue || 'all');
      return;
    }
    const assetHeatClose = event.target?.closest?.('[data-asset-heat-close]');
    if (assetHeatClose) {
      event.preventDefault();
      const panel = assetHeatClose.closest('[data-asset-heatmap-panel]');
      closeTradeReviewAssetFloat(panel);
      return;
    }
    const assetHeatButton = event.target?.closest?.('[data-asset-heat-select]');
    if (assetHeatButton) {
      event.preventDefault();
      const panel = assetHeatButton.closest('[data-asset-heatmap-panel]');
      const target = assetHeatButton.dataset.assetHeatSelect;
      if (!panel || !target) return;
      panel.querySelectorAll('[data-asset-heat-select]').forEach((button) => {
        const active = button.dataset.assetHeatSelect === target;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      let activeDetail = null;
      panel.querySelectorAll('[data-asset-heat-detail]').forEach((detail) => {
        const active = detail.dataset.assetHeatDetail === target;
        detail.hidden = !active;
        if (active) activeDetail = detail;
      });
      const floatLayer = panel.querySelector('[data-asset-heat-float]');
      if (floatLayer && activeDetail) {
        floatLayer.hidden = false;
        document.body.classList.add('trade-review-asset-open');
        const floatCard = floatLayer.querySelector('.tradeReviewAssetFloatCard');
        if (floatCard) floatCard.scrollTop = 0;
        floatLayer.querySelector('[data-asset-heat-close]')?.focus?.({ preventScroll: true });
      }
      return;
    }
    const reviewButton = event.target?.closest?.('[data-trade-review]');
    if (reviewButton) {
      try {
        await openTradeReviewById(reviewButton.dataset.tradeReview);
      } catch (err) {
        showError(err);
      }
      return;
    }
    if (event.target?.id === 'btnOpenTradeForm') {
      const panel = $('manualTradeEntry');
      panel.hidden = !panel.hidden;
      if (panel.hidden) return;
      panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setTimeout(() => $('tradeName')?.focus(), 250);
      return;
    }
    if (event.target?.id === 'btnCloseTradeForm') {
      $('manualTradeEntry').hidden = true;
      return;
    }
    const filterButton = event.target?.closest?.('#btnToggleTradeFilters');
    if (filterButton) {
      event.preventDefault();
      event.stopPropagation();
      toggleTradeFilterPanel();
      return;
    }
    if (event.target?.id === 'btnClearTradeFilters') {
      event.preventDefault();
      setDefaultFilters();
      closeBottomSheet();
      loadLedger().catch(showError);
      return;
    }
    if (event.target?.id === 'btnShowIdentityTransition') {
      event.preventDefault();
      const form = event.target.closest('#tradeReviewForm');
      if (form) {
        updateTransitionSection(form, { forceShow: true });
        updateTradeReviewCompletion(form);
      }
      return;
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const floatLayer = document.querySelector('[data-asset-heat-float]:not([hidden])');
    if (!floatLayer) return;
    event.preventDefault();
    closeTradeReviewAssetFloat(floatLayer.closest('[data-asset-heatmap-panel]'));
  });
  document.addEventListener('change', (event) => {
    const form = event.target?.closest?.('#tradeReviewForm');
    const choice = event.target?.closest?.('.tradeReviewTag input');
    if (!choice) {
      if (form) {
        updateTransitionSection(form);
        updateTradeReviewCompletion(form);
      }
      return;
    }
    if (choice.type === 'radio') {
      choice.form?.querySelectorAll(`input[name="${choice.name}"]`).forEach((input) => {
        input.closest('.tradeReviewTag')?.classList.toggle('checked', Boolean(input.checked));
      });
      if (['reviewInitialIdentity', 'reviewCurrentIdentity'].includes(choice.name)) {
        applyIdentityDefaults(choice.form, { forceRule: true, forceLimit: true, forcePeriod: choice.name === 'reviewInitialIdentity' });
      } else {
        updateTransitionSection(choice.form);
        updateTradeReviewCompletion(choice.form);
      }
      return;
    }
    if (choice.name === 'reviewTag' && choice.checked) {
      const checkedTags = choice.form?.querySelectorAll('input[name="reviewTag"]:checked').length || 0;
      if (checkedTags > 3) {
        choice.checked = false;
        if ($('tradeReviewMsg')) $('tradeReviewMsg').textContent = '复盘标签最多选择 3 个。';
      }
    }
    choice.closest('.tradeReviewTag')?.classList.toggle('checked', Boolean(choice.checked));
    updateTradeReviewCompletion(choice.form);
  });
  document.addEventListener('input', (event) => {
    const form = event.target?.closest?.('#tradeReviewForm');
    if (!form) return;
    if (['reviewMaxPositionPct', 'reviewSellCondition', 'reviewNextReviewDate', 'reviewAddCondition'].includes(event.target.id)) {
      event.target.dataset.autoGenerated = '0';
      updateTransitionSection(form);
    }
    updateTradeReviewCompletion(form);
  });
  document.addEventListener('submit', (event) => {
    if (event.target?.id === 'tradeFilterForm') {
      event.preventDefault();
      state.filters = readTradeFilterForm(event.target);
      state.showAll = false;
      closeBottomSheet();
      loadLedger().catch(showError);
      return;
    }
    if (event.target?.id !== 'tradeReviewForm') return;
    event.preventDefault();
    saveTradeReview(event.target).catch(showError);
  });
}

async function init() {
  setActiveNav();
  bindEvents();
  setDefaultFilters();
  await loadOptions();
  setDefaultFilters();
  await loadLedger();
}

init().catch(showError);
