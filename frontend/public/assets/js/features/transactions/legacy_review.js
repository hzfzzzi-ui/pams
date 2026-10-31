import { accountLabel } from '../../api.js?v=20261031-gray-v1';
import { escapeHtml, formatPlainMoney as money, readOptionalNumber as readNumber } from '../../utils.js?v=20261031-gray-v1';

export function createLegacyReview({state, scope}) {
const $ = scope.byId;
const readOptionalNumber = id => readNumber(typeof id === "string" ? $(id) : id);
// Preserve the existing rules, review validation and historical calculations.
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

const TRADE_REVIEW_ACTION_PATTERN_KEYS = new Set(['review_tags', 'buy_reason', 'sell_reason']);


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

function formatCampaignMoney(value, currency) {
  if (value == null || value === '') return '--';
  const n = Number(value);
  if (!Number.isFinite(n)) return '--';
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}${money(Math.abs(n), currency || '')}`;
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
  const actual = Math.abs(tradeReviewNumber(asset?.actual_total_pnl_cny, NaN));
  const effect = Math.abs(tradeReviewNumber(asset?.estimated_decision_effect_cny, 0));
  const weight = Math.abs(tradeReviewNumber(asset?.weight_cny, 0));
  const count = Math.max(0, tradeReviewNumber(asset?.trade_count, 0));
  if (Number.isFinite(actual) && actual > 0.01) return actual;
  if (effect > 0.01) return effect;
  if (weight > 0.01) return weight;
  return Math.max(count * 120, 1);
}

function tradeReviewAssetPrimaryPnl(asset) {
  const actual = tradeReviewNumber(asset?.actual_total_pnl_cny, NaN);
  if (Number.isFinite(actual)) return actual;
  return tradeReviewNumber(asset?.estimated_decision_effect_cny, 0);
}

function tradeReviewAssetTone(asset) {
  const tone = String(asset?.impact_tone || '').toLowerCase();
  if (['gain', 'loss', 'flat'].includes(tone)) return tone;
  return tradeReviewHeatTone(tradeReviewAssetPrimaryPnl(asset));
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

function tradeReviewAssetStatusText(asset) {
  if (asset?.attribution_status === 'partial_with_actual_pnl') return '真实收益已接入';
  return '仅动作时点';
}

function tradeReviewAssetBreakdownValue(item) {
  if (item?.value_cny == null || item?.value_cny === '') return '待接入';
  return formatCnyEffect(item.value_cny);
}

function tradeReviewAssetBreakdownTone(item) {
  if (item?.value_cny == null || item?.value_cny === '') return 'flat';
  return pctTone(item.value_cny);
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

function tradeReviewPatternReasonFold(examples, opts = {}) {
  const rows = (examples || [])
    .filter((trade) => trade?.trade_id)
    .slice(0, opts.limit || 5);
  if (!rows.length) return '';
  const compactClass = opts.compact ? ' compact' : '';
  return `
    <details class="tradeReviewPatternReasonFold${compactClass}">
      <summary>
        <strong>展开标的原因</strong>
        <span>${escapeHtml(rows.length)} 笔</span>
      </summary>
      <div class="tradeReviewPatternReasonList">
        ${rows.map((trade) => {
          const tone = tradeReviewTradeTone(trade);
          return `
            <button type="button" class="tradeReviewPatternReasonTrade ${tone}" data-trade-review="${escapeHtml(String(trade.trade_id || ''))}">
              <div>
                <strong>${escapeHtml(trade.name || trade.symbol || '--')}</strong>
                <span>${escapeHtml(trade.trade_date || '--')} · ${escapeHtml(sideLabel(trade.side))} · ${escapeHtml(trade.auto_label || trade.contribution_label || '自动归因')}</span>
                <p>${escapeHtml(tradeReviewTradeReason(trade))}</p>
              </div>
              <em class="${pctTone(trade.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(trade.estimated_decision_effect_cny))}</em>
            </button>
          `;
        }).join('')}
      </div>
    </details>
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

function reviewTextValue(value) {
  return escapeHtml(String(value || ''));
}

function horizonMetric(metrics, key) {
  return metrics?.horizons?.[key]?.return_pct;
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

return {todayIso, defaultFilterState, holdingDisplay, sideLabel, sideClass, sourceLabel, reviewOptionList, reviewOptionLabel, reviewStrictOptionLabel, isReviewableTrade, reviewSummaryParts, reviewSummaryText, formatPctPoint, pctTone, autoConfidenceLabel, autoLevelTone, autoReviewTagLabel, autoSuggestedTagButtons, sideWithStatus, option, reviewAttributionLabel, formatCampaignMoney, workbenchTone, formatCnyEffect, formatCnyEffectCompact, tradeReviewNumber, tradeReviewHeatTone, tradeReviewActionPatterns, formatDecisionRate, tradeReviewAssetName, tradeReviewAssetMeta, tradeReviewAssetCleanName, tradeReviewCompactSymbol, tradeReviewAssetShortLabel, tradeReviewAssetTileText, tradeReviewAssetFullLabel, tradeReviewAssetWeight, tradeReviewAssetPrimaryPnl, tradeReviewAssetTone, tradeReviewAssetTiles, tradeReviewTreemapWeight, tradeReviewTreemapSplit, tradeReviewTreemapPartition, tradeReviewTreemapLayout, tradeReviewAssetTileClass, tradeReviewClamp, tradeReviewAssetTileStyle, tradeReviewAssetTags, tradeReviewTradeTone, tradeReviewContributionLabel, tradeReviewTradeMoveLabel, tradeReviewTradeReason, tradeReviewAssetStatusText, tradeReviewAssetBreakdownValue, tradeReviewAssetBreakdownTone, tradeReviewTradeTags, tradeReviewDirectionalTrades, tradeReviewDirectionSort, tradeReviewDirectionMatchesFilter, tradeReviewDirectionTradeRow, tradeReviewDirectionFilterButton, tradeReviewPatternTiles, tradeReviewHeatTileStyle, tradeReviewPatternReasonFold, findTradeRow, tradeRowFromAttributionPayload, reviewTextValue, horizonMetric, metricAvailable, autoEvidenceRows, addDaysIso, identityRule, ruleLabel, setRadioValue, formInputValue, setGeneratedValue, selectedIdentity, isTransitionVisible, isClosedPosition, formatCompletion, updateTradeReviewCompletion, updateTransitionSection, applyIdentityDefaults, tradeReviewQuality, tradeReviewQualityV2, optionalInputValue, radioInputValue, selectedCheckboxValues, validateTradeReviewForm, REVIEW_TAG_GROUP_LABELS, HOLDING_PERIOD_OPTIONS, POSITION_IDENTITY_RULES, COMMON_REVIEW_TAG_VALUES, REVIEW_TAG_LABEL_OVERRIDES, REVIEW_VALUE_LABELS, TRADE_REVIEW_ACTION_PATTERN_KEYS};
}
