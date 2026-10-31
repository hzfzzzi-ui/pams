// Existing ledger mappings, inference and aggregation kept verbatim for compatibility.
// This module adds no financial/classification rules; migrate these only with backend contracts.
import { accountLabel } from '../../api.js?v=20261031-modules-v1';
import { escapeHtml, formatCurrency, formatMoneyAxis as axisMoney, formatMoneyFixed2 as money, formatPercent, formatSignedMoneyFixed2 as signedMoney, toFiniteNumber as num } from '../../utils.js?v=20261031-modules-v1';

const ACCOUNT_GROUPS = [
  { code: 'ccb', label: '建设银行', children: ['ccb_7332', 'ccb_credit_0453', 'ccb_yhs300_fund'] },
  { code: 'icbc_6689', label: '工商银行', children: ['icbc_6689_cny', 'icbc_6689_usd'], forceChildren: true },
  { code: 'wechat_root', label: '微信', children: ['wechat', 'wechat_lqt', 'tencent_licaitong'] },
  { code: 'guangfa_root', label: '广发银行', children: ['gf_7443', 'gf_9711'] },
  { code: 'santander_root', label: 'Santander', children: ['santander'], forceChildren: true },
  { code: 'alipay_root', label: '支付宝', children: ['alipay'], standalone: true },
  { code: 'ibkr_root', label: 'IBKR', children: ['ibkr'], standalone: true },
  { code: 'tonghuashun_root', label: '同花顺', children: ['tonghuashun'], standalone: true },
  {
    code: 'benefits_root',
    label: '五险一金',
    children: [
      'pension_insurance',
      'medical_insurance',
      'unemployment_insurance',
      'work_injury_insurance',
      'maternity_insurance',
      'hpf',
    ],
  },
];

const ACCOUNT_GROUP_CHILD_CODES = new Set(ACCOUNT_GROUPS.flatMap((group) => group.children));

const ACCOUNT_SUBACCOUNT_LABELS = {
  ccb_7332: '储蓄卡',
  ccb_credit_0453: '信用卡',
  ccb_yhs300_fund: '建行理财',
  wechat: '零钱',
  wechat_lqt: '零钱通',
  tencent_licaitong: '腾讯理财通',
  icbc_6689_cny: '6689 人民币',
  icbc_6689_usd: '6689 美元',
  gf_7443: '7443 信用卡',
  gf_9711: '9711 信用卡',
  santander: '储蓄卡',
  pension_insurance: '养老保险',
  medical_insurance: '医疗保险',
  unemployment_insurance: '失业保险',
  work_injury_insurance: '工伤保险',
  maternity_insurance: '生育保险',
  hpf: '公积金',
};

const TRANSFER_ENDPOINT_LABELS = {
  ccb_7332: '建行7332账户',
  hpf: '公积金账户',
};

const ACCOUNT_KIND_OVERRIDES = {
  ccb_yhs300_fund: 'investment',
  tencent_licaitong: 'investment',
  ibkr: 'investment',
  tonghuashun: 'investment',
  pension_insurance: 'benefit',
  medical_insurance: 'benefit',
  unemployment_insurance: 'benefit',
  work_injury_insurance: 'benefit',
  maternity_insurance: 'benefit',
  hpf: 'benefit',
};

const ACCOUNT_NUMERIC_FIELDS = [
  'income',
  'expense',
  'transfer',
  'investment_inflow',
  'investment_outflow',
  'investment_return',
  'investment_fee',
  'net',
  'row_count',
  'pending_count',
  'activity',
];

const LEDGER_EDIT_TYPES = [
  ['operating_expense', '消费'],
  ['operating_income', '收入'],
  ['investment_funding', '投资入金'],
  ['investment_withdrawal', '投资出金'],
  ['internal_transfer', '内部转账'],
  ['debt_repayment', '信用卡还款'],
];

const LEDGER_EDIT_DEFAULTS = {
  operating_expense: { category: '其他支出', subcategory: '手动确认' },
  operating_income: { category: '其他收入', subcategory: '手动确认' },
  investment_funding: { category: '内部划转', subcategory: '投资入金' },
  investment_withdrawal: { category: '内部划转', subcategory: '投资出金' },
  internal_transfer: { category: '内部划转', subcategory: '内部转账' },
  debt_repayment: { category: '内部划转', subcategory: '信用卡还款' },
};

const LEDGER_EDIT_CATEGORY_OPTIONS = {
  operating_expense: ['餐饮', '交通出行', '居住', '通讯网络', '工具订阅', '购物', '医疗健康', '娱乐休闲', '学习成长', '人情往来', '借还款/往来', '其他支出', '待确认支出'],
  operating_income: ['工资薪酬', '奖金', '报销补贴', '福利保障', '退款返现', '人情往来收入', '借还款/往来', '利息收入', '其他收入'],
  investment_funding: ['内部划转'],
  investment_withdrawal: ['内部划转'],
  internal_transfer: ['内部划转'],
  debt_repayment: ['内部划转'],
};

const LEDGER_EDIT_SUBCATEGORY_OPTIONS = {
  餐饮: ['餐饮', '工作餐', '咖啡饮品', '外卖', '聚餐', '手动确认'],
  交通出行: ['打车', '公共交通', '停车', '机票火车', '手动确认'],
  居住: ['房租', '物业水电', '网络通讯', '家居维修', '手动确认'],
  通讯网络: ['手机话费', '宽带网络', '流量套餐', '手动确认'],
  工具订阅: ['工具订阅', '美图订阅', 'AI订阅', '软件会员', '云服务', '手动确认'],
  购物: ['日常购物', '服饰美妆', '数码配件', '超市便利', '手动确认'],
  医疗健康: ['医保缴费', '医院诊疗', '药品保健', '体检', '手动确认'],
  娱乐休闲: ['影音会员', '游戏娱乐', '演出票务', '运动休闲', '手动确认'],
  学习成长: ['课程培训', '图书资料', '考试认证', '手动确认'],
  人情往来: ['红包转账', '礼金', '代付代收', '手动确认'],
  '借还款/往来': ['借款', '还款', '往来款', '手动确认'],
  其他支出: ['其他支出', '手动确认'],
  待确认支出: ['手动确认'],
  工资薪酬: ['工资', '薪金', '手动确认'],
  奖金: ['年终奖', '绩效奖金', '手动确认'],
  报销补贴: ['差旅报销补助', '费用报销', '补贴', '手动确认'],
  福利保障: ['公积金', '医保', '社保', '手动确认'],
  退款返现: ['消费退款', '返现', '退货退款', '手动确认'],
  人情往来收入: ['红包收入', '转账收入', '礼金收入', '手动确认'],
  利息收入: ['利息', '现金收益', '手动确认'],
  其他收入: ['其他收入', '手动确认'],
  内部划转: ['换汇转账', '转账退回', '投资入金', '投资出金', '内部转账', '信用卡还款', '资金在途', '手动确认'],
};

export function createLedgerLegacyModel(state) {
function flowLabel(flowType) {
  const labels = {
    income: '收入',
    expense: '消费',
    transfer: '资金流转',
    investment_return: '投资收益',
    investment_inflow: '投资流入',
    investment_outflow: '投资流出',
    investment_fee: '投资费用',
    investment_transfer: '投资划转',
  };
  return labels[flowType] || flowType || '未知';
}

function flowClass(flowType) {
  if (flowType === 'income') return 'income';
  if (flowType === 'expense') return 'expense';
  if (flowType === 'transfer') return 'transfer';
  if (String(flowType || '').startsWith('investment_')) return 'transfer';
  return 'other';
}

function compactText(value, maxLength = 24) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  return text.length > maxLength ? `${text.slice(0, Math.max(1, maxLength - 1))}…` : text;
}

function stripLongDetail(value) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  return text
    .split(/[；;]/)[0]
    .split(/\s\/\s/)[0]
    .trim();
}

function accountFriendlyName(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const apiLabel = accountLabel(raw);
  if (apiLabel && apiLabel !== raw) return apiLabel;
  if (ACCOUNT_SUBACCOUNT_LABELS[raw]) return ACCOUNT_SUBACCOUNT_LABELS[raw];
  const displayName = accountDisplayName(raw);
  return displayName && displayName !== raw ? displayName : raw;
}

function accountShortLabel(value) {
  return compactText(accountFriendlyName(value), 16);
}

function counterpartyShortLabel(row) {
  const raw = String(row.counterparty || '').trim();
  if (!raw) return '';
  return compactText(accountDisplayName(raw) || accountLabel(raw) || raw, 18);
}

function rowCategoryText(row) {
  return String(row.coarse_category || row.subcategory || row.category || '').trim();
}

function cashflowTypeText(row) {
  return String(row.cashflow_type || '').toLowerCase();
}

function isEditableLedgerRow(row) {
  return /^\d+$/.test(String(row?.id || ''));
}

function ledgerEditType(row) {
  const cashflowType = cashflowTypeText(row);
  if (LEDGER_EDIT_DEFAULTS[cashflowType]) return cashflowType;
  if (row?.flow_type === 'income') return 'operating_income';
  if (row?.flow_type === 'expense') return 'operating_expense';
  if (row?.flow_type === 'transfer') return 'internal_transfer';
  return 'operating_expense';
}

function ledgerEditTypeLabel(cashflowType) {
  return LEDGER_EDIT_TYPES.find(([value]) => value === cashflowType)?.[1] || cashflowType || '未分类';
}

function ledgerEditActionLabel(row) {
  return row?.needs_review ? '确认/修改' : '修改分类';
}

function ledgerEditSubmitLabel(row) {
  return row?.needs_review ? '保存并确认' : '保存修改';
}

function isAccountValidationRow(row) {
  const text = [row.summary, row.description, row.counterparty].filter(Boolean).join(' ').toLowerCase();
  const amount = Math.abs(num(row.amount_cny ?? row.amount));
  return amount <= 1 && (text.includes('validacion') || text.includes('validación') || text.includes('validation') || text.includes('账户验证'));
}

function isInvestmentFundingRow(row) {
  return cashflowTypeText(row) === 'investment_funding' || String(row.flow_type || '').toLowerCase() === 'investment_inflow';
}

function isInvestmentWithdrawalRow(row) {
  return cashflowTypeText(row) === 'investment_withdrawal' || String(row.flow_type || '').toLowerCase() === 'investment_outflow';
}

function routeEndpointLabel(value, maxLength = 14) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const label = transferEndpointName(raw) || accountFriendlyName(raw);
  if (!label || label === '-') return '';
  return compactText(label, maxLength);
}

function rowTagLabel(row) {
  if (isInvestmentFundingRow(row)) return '投资入金';
  if (isInvestmentWithdrawalRow(row)) return '投资出金';
  return flowLabel(row.flow_type);
}

function rowAmountClass(row, amount) {
  const flowType = String(row.flow_type || '').toLowerCase();
  if (isInvestmentFundingRow(row) || isInvestmentWithdrawalRow(row) || flowType === 'transfer' || flowType === 'investment_transfer') {
    return 'transfer';
  }
  return amount >= 0 ? 'up' : 'down';
}

function rowTitle(row) {
  const route = transferRoute(row);
  if (route?.label === '投资入金') {
    const target = routeEndpointLabel(route.to, 10);
    return target ? `${target} 入金` : '投资入金';
  }
  if (route?.label === '投资出金') {
    const source = routeEndpointLabel(route.from, 10);
    return source ? `${source} 出金` : '投资出金';
  }
  if (route?.label === '内部转账') return '内部转账';

  const cashflowType = cashflowTypeText(row);
  if (cashflowType === 'investment_funding') return '投资入金';
  if (cashflowType === 'investment_withdrawal') return '投资出金';

  if (isAccountValidationRow(row)) return '账户验证';

  const category = rowCategoryText(row);
  if (String(row.flow_type || '') === 'income') {
    if (category.includes('工资')) return '工资入账';
    if (category.includes('公积金')) return '公积金汇缴';
  }

  const summary = stripLongDetail(row.summary);
  const fallback = category || row.counterparty || flowLabel(row.flow_type);
  return compactText(summary || fallback || '未命名流水', 24);
}

function rowMetaText(row) {
  const route = transferRoute(row);
  if (route) {
    const from = routeEndpointLabel(route.from, 11);
    const to = routeEndpointLabel(route.to, 11);
    return from && to ? `${from} → ${to}` : (from || to || rowCategoryText(row) || route.label);
  }

  return accountShortLabel(row.account_code);
}

function rowDateText(row) {
  return row.transaction_date || '';
}

function rowSearchText(row) {
  return [
    row.summary,
    row.description,
    row.counterparty,
    row.counter_account_code,
    row.category,
    row.subcategory,
    row.coarse_category,
    flowLabel(row.flow_type),
    accountLabel(row.account_code),
    row.account_code,
  ].filter(Boolean).join(' ').toLowerCase();
}

function recentAmountValue(row) {
  return Math.abs(num(row?.amount_cny ?? row?.amount));
}

function sortRecentRows(rows) {
  const mode = state.recentAmountSort || '';
  if (!mode) return rows;
  const direction = mode === 'amount_asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const amountDelta = recentAmountValue(a) - recentAmountValue(b);
    if (amountDelta !== 0) return amountDelta * direction;
    return String(b.transaction_date || '').localeCompare(String(a.transaction_date || ''));
  });
}

function originalCurrencyText(row) {
  const currency = String(row.currency || '').trim().toUpperCase();
  if (!currency || currency === 'CNY') return '';
  if (row.amount === null || row.amount === undefined || row.amount === '') return '';
  return formatCurrency(row.amount, currency, { signed: true });
}

function inferInvestmentAccountCode(row) {
  const transferGroupId = String(row.transfer_group_id || '').toLowerCase();
  const cashflowType = cashflowTypeText(row);
  const text = [row.account_code, row.counterparty, row.summary, row.description, row.category, row.subcategory]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();

  if (text.includes('156010860') || (cashflowType === 'investment_withdrawal' && text.includes('赎回账户'))) return 'tencent_licaitong';

  if (transferGroupId.includes('to_ibkr')) return 'ibkr';
  if (transferGroupId.includes('to_tonghuashun')) return 'tonghuashun';
  if (transferGroupId.includes('to_tencent_licaitong')) return 'tencent_licaitong';
  if (transferGroupId.includes('direct_investment') || transferGroupId.includes('yhs300')) return 'ccb_yhs300_fund';

  if (text.includes('interactive brokers') || text.includes('ibkr')) return 'ibkr';
  if (text.includes('同花顺') || text.includes('tonghuashun')) return 'tonghuashun';
  if (text.includes('理财通') || text.includes('licaitong')) return 'tencent_licaitong';
  if (text.includes('建行理财') || text.includes('易沪深300') || text.includes('110020')) return 'ccb_yhs300_fund';
  return '';
}

function transferEndpointName(value) {
  const raw = String(value || '').trim();
  if (!raw) return '-';
  if (TRANSFER_ENDPOINT_LABELS[raw]) return TRANSFER_ENDPOINT_LABELS[raw];
  return accountFriendlyName(raw) || raw;
}

function isImporterCounterparty(value) {
  const raw = String(value || '').trim().toLowerCase();
  return raw === 'tradingview' || raw === 'pams_tradingview_transactions';
}

function investmentFundingSourceAccount(row, counterparty) {
  if (!isImporterCounterparty(counterparty)) return counterparty;
  if (String(row.account_code || '').toLowerCase() === 'tonghuashun') return 'ccb_7332';
  return '';
}

function transferRoute(row) {
  const flowType = String(row.flow_type || '').toLowerCase();
  const cashflowType = String(row.cashflow_type || '').toLowerCase();
  const transferGroupId = String(row.transfer_group_id || '').toLowerCase();
  if (transferGroupId.startsWith('payment_proxy:')) return null;
  const amount = num(row.amount_cny ?? row.amount);
  const account = row.account_code || '';
  const counterAccount = row.counter_account_code || '';
  const counterparty = row.counterparty || '';

  if (cashflowType === 'investment_funding' || flowType === 'investment_inflow') {
    const investmentAccount = inferInvestmentAccountCode(row) || (flowType === 'investment_inflow' ? account : '');
    const fundingSource = investmentFundingSourceAccount(row, counterparty);
    if (flowType === 'investment_inflow' || row.account_kind === 'investment' || amount >= 0) {
      return { label: '投资入金', from: fundingSource || counterparty, to: investmentAccount || account };
    }
    return { label: '投资入金', from: account, to: investmentAccount || counterparty };
  }

  if (cashflowType === 'investment_withdrawal' || flowType === 'investment_outflow') {
    const investmentAccount = inferInvestmentAccountCode(row) || (flowType === 'investment_outflow' ? account : '');
    if (flowType === 'investment_outflow' || row.account_kind === 'investment' || amount < 0) {
      return { label: '投资出金', from: investmentAccount || account, to: counterparty };
    }
    return { label: '投资出金', from: investmentAccount || counterparty, to: account };
  }

  if (flowType === 'transfer' && (counterAccount || counterparty)) {
    const transferPeer = counterAccount || counterparty;
    return amount < 0
      ? { label: '内部转账', from: account, to: transferPeer }
      : { label: '内部转账', from: transferPeer, to: account };
  }

  return null;
}

function uniqueOptionValues(values) {
  return [...new Set(values.map((value) => String(value || '').trim()).filter(Boolean))];
}

function ledgerCategoryOptions(cashflowType, currentValue = '') {
  return uniqueOptionValues([
    ...(LEDGER_EDIT_CATEGORY_OPTIONS[cashflowType] || []),
    currentValue,
  ]);
}

function ledgerSubcategoryOptions(category, currentValue = '') {
  return uniqueOptionValues([
    ...(LEDGER_EDIT_SUBCATEGORY_OPTIONS[category] || ['手动确认']),
    currentValue,
  ]);
}

function overviewPeriodKey() {
  return state.overviewPeriod === 'month' ? 'month' : 'ytd';
}

function overviewPeriodLabel() {
  return overviewPeriodKey() === 'month' ? '本月至今' : '本年至今';
}

function currentOverviewPeriod(data) {
  return data.periods?.[overviewPeriodKey()] || {};
}

function currentOverviewTransferSummary(data) {
  if (overviewPeriodKey() === 'month') return data.transfer_summary_month || data.transfer_summary || [];
  return data.transfer_summary_ytd || data.transfer_summary || [];
}

function overviewDisposableNet(period) {
  return num(period.disposable_net ?? period.net ?? 0);
}

function overviewInvestmentTransfer(data, period = currentOverviewPeriod(data)) {
  return num(period.investment_transfer ?? investmentTransferAmount(currentOverviewTransferSummary(data)));
}

function overviewInvestmentTransferNet(data, period = currentOverviewPeriod(data)) {
  if (period.investment_transfer_net !== undefined && period.investment_transfer_net !== null) {
    return num(period.investment_transfer_net);
  }
  return -Math.abs(overviewInvestmentTransfer(data, period));
}

function overviewOtherTransfer(data, period = currentOverviewPeriod(data)) {
  if (period.other_transfer !== undefined && period.other_transfer !== null) return num(period.other_transfer);
  const totalTransfer = num(period.internal_transfer || 0);
  return Math.max(totalTransfer - overviewInvestmentTransfer(data, period), 0);
}

function overviewCashNet(data, period = currentOverviewPeriod(data)) {
  if (period.cash_net !== undefined && period.cash_net !== null) return num(period.cash_net);
  return overviewDisposableNet(period) + overviewInvestmentTransferNet(data, period);
}

function currentOverviewCategories(data, flowType) {
  const period = overviewPeriodKey();
  const keys = {
    month: {
      expense: 'expense_categories_month',
      income: 'income_categories_month',
    },
    ytd: {
      expense: 'expense_categories_ytd',
      income: 'income_categories_ytd',
    },
  };
  const fallbackKey = flowType === 'income' ? 'income_categories' : 'expense_categories';
  const key = keys[period]?.[flowType];
  return (key && data[key]) || data[fallbackKey] || [];
}

function overviewFlowKey() {
  return state.overviewFlow === 'expense' ? 'expense' : 'income';
}

function overviewFlowLabel(flowType = overviewFlowKey()) {
  return flowType === 'expense' ? '消费' : '收入';
}

function accountDisplayName(accountCode) {
  const account = (state.accounts?.accounts || []).find((item) => item.account_code === accountCode);
  return account?.display_name || account?.subaccount_label || accountLabel(accountCode) || accountCode || '未命名账户';
}

function subAccountDisplayName(accountCode) {
  return ACCOUNT_SUBACCOUNT_LABELS[accountCode] || accountDisplayName(accountCode);
}

function accountOverviewMap() {
  return new Map((state.accounts?.accounts || [])
    .filter((account) => account?.account_code)
    .map((account) => [account.account_code, account]));
}

function accountOverview(accountCode) {
  return accountOverviewMap().get(accountCode) || null;
}

function accountPrimaryBalance(accountCode) {
  const account = accountOverview(accountCode);
  const balance = account?.primary_balance || null;
  if (!balance || balance.amount === null || balance.amount === undefined || balance.amount === '') {
    return null;
  }
  return {
    amount: num(balance.amount),
    currency: String(balance.currency || account?.base_currency || 'CNY').toUpperCase(),
    as_of: balance.as_of || null,
  };
}

function hasAccountBalance(accountCode) {
  return Boolean(accountPrimaryBalance(accountCode));
}

function accountBalanceItems(rows = []) {
  const totals = new Map();
  (rows || []).forEach((row) => {
    const balance = accountPrimaryBalance(row.account_code);
    if (!balance) return;
    const current = totals.get(balance.currency) || { currency: balance.currency, amount: 0, as_of: null };
    current.amount += balance.amount;
    current.as_of = maxSyncAt(current.as_of, balance.as_of);
    totals.set(balance.currency, current);
  });
  return [...totals.values()]
    .filter((item) => Math.abs(item.amount) > 0.000001 || item.as_of)
    .sort((a, b) => {
      if (a.currency === 'CNY') return -1;
      if (b.currency === 'CNY') return 1;
      return a.currency.localeCompare(b.currency);
    });
}

function formatBalanceItem(item) {
  return formatCurrency(item.amount, item.currency, { maximumFractionDigits: 2 });
}

function accountBalanceSummary(rows = []) {
  const items = accountBalanceItems(rows);
  const balanceLabel = rows.length && rows.every((row) => normalizeAccountRow(row).account_kind === 'investment')
    ? '净值'
    : '余额';
  if (!items.length) {
    return { value: '--', meta: `${balanceLabel}待核准`, tone: 'is-missing' };
  }
  const shown = items.slice(0, 2).map(formatBalanceItem).join(' / ');
  const suffix = items.length > 2 ? ` +${items.length - 2}` : '';
  const latestAsOf = items.reduce((latest, item) => maxSyncAt(latest, item.as_of), null);
  const currencyText = items.length > 1 ? `${items.length}币种` : items[0].currency;
  const tone = items.some((item) => item.amount < 0) ? 'down' : '';
  return {
    value: `${shown}${suffix}`,
    meta: latestAsOf ? `${balanceLabel} ${formatSyncDate(latestAsOf)} · ${currencyText}` : `${balanceLabel} · ${currencyText}`,
    tone,
  };
}

function accountPlatformBalances(accountCode) {
  const account = accountOverview(accountCode);
  return Array.isArray(account?.platform_balances) ? account.platform_balances : [];
}

function platformBalanceAmount(item) {
  return num(item?.market_value ?? item?.amount ?? item?.value ?? 0);
}

function addCurrencyAmount(map, currency, amount) {
  const key = String(currency || 'CNY').toUpperCase();
  const current = map.get(key) || { currency: key, amount: 0 };
  current.amount += amount;
  map.set(key, current);
}

function sortedCurrencyItems(map) {
  return [...map.values()]
    .filter((item) => Math.abs(item.amount) > 0.000001)
    .sort((a, b) => {
      if (a.currency === 'CNY') return -1;
      if (b.currency === 'CNY') return 1;
      return Math.abs(b.amount) - Math.abs(a.amount);
    });
}

function formatCurrencyItems(items, { absolute = false, signed = false } = {}) {
  if (!items.length) return '';
  const shown = items.slice(0, 2).map((item) => formatCurrency(
    absolute ? Math.abs(item.amount) : item.amount,
    item.currency,
    { maximumFractionDigits: 2, signed },
  )).join(' / ');
  return items.length > 2 ? `${shown} +${items.length - 2}` : shown;
}

function formatCurrencyItemsCompact(items, { absolute = false, signed = false } = {}) {
  if (!items.length) return '';
  if (items.length > 1) return `${items.length} 币种`;
  return formatCurrency(
    absolute ? Math.abs(items[0].amount) : items[0].amount,
    items[0].currency,
    { maximumFractionDigits: 2, signed },
  );
}

function accountInvestmentBalanceEntries(accountCode) {
  const assets = new Map();
  const liabilities = new Map();
  const holdingPnl = new Map();
  accountPlatformBalances(accountCode).forEach((item) => {
    const currency = item.currency || 'CNY';
    const amount = platformBalanceAmount(item);
    const liability = String(item.asset_liability || '').includes('负债') || amount < 0;
    if (liability) {
      addCurrencyAmount(liabilities, currency, amount);
    } else {
      addCurrencyAmount(assets, currency, amount);
    }
    addCurrencyAmount(holdingPnl, currency, num(item.holding_pnl || 0));
  });

  const entries = [];
  const assetItems = sortedCurrencyItems(assets);
  const liabilityItems = sortedCurrencyItems(liabilities);
  const pnlItems = sortedCurrencyItems(holdingPnl);
  if (liabilityItems.length) {
    entries.push({ label: '融资负债', value: formatCurrencyItemsCompact(liabilityItems, { absolute: true }), tone: 'risk' });
  }
  if (pnlItems.length) {
    const pnlTotal = pnlItems.reduce((sum, item) => sum + item.amount, 0);
    entries.push({ label: '持仓盈亏', value: formatCurrencyItemsCompact(pnlItems, { signed: true }), tone: pnlTotal >= 0 ? 'up' : 'down' });
  }
  return entries;
}

function investmentAccountStatsEntries(row) {
  const normalized = normalizeAccountRow(row);
  const netFunding = normalized.investment_inflow - normalized.investment_outflow;
  const fundingLabel = normalized.funding_scope === 'personal_capital' ? '个人入金' : '净入金';
  const hasOutflow = Math.abs(normalized.investment_outflow) > 0.000001;
  const entries = [];
  if (Math.abs(netFunding) > 0.000001) entries.push({ label: fundingLabel, value: signedMoney(netFunding), tone: netFunding >= 0 ? 'up' : 'down' });
  if (hasOutflow && Math.abs(normalized.investment_inflow) > 0.000001) entries.push({ label: '入金', value: money(normalized.investment_inflow), tone: 'data' });
  if (hasOutflow) entries.push({ label: '出金', value: money(normalized.investment_outflow), tone: 'data' });
  if (Math.abs(normalized.investment_return) > 0.000001) entries.push({ label: '现金收益', value: money(normalized.investment_return), tone: 'up' });
  if (Math.abs(normalized.investment_fee) > 0.000001) entries.push({ label: '费用', value: money(normalized.investment_fee), tone: 'down' });
  return entries;
}

function normalizeAccountRow(row = {}) {
  const normalized = {
    ...row,
    account_code: row.account_code || '',
    account_kind: row.account_kind || ACCOUNT_KIND_OVERRIDES[row.account_code] || 'cash',
    latest_sync_at: row.latest_sync_at || row.imported_at || null,
  };
  ACCOUNT_NUMERIC_FIELDS.forEach((field) => {
    normalized[field] = num(row[field] || 0);
  });
  return normalized;
}

function maxSyncAt(current, candidate) {
  if (!candidate) return current || null;
  if (!current) return candidate;
  return String(candidate) > String(current) ? candidate : current;
}

function emptyAccountRow(accountCode) {
  return normalizeAccountRow({
    account_code: accountCode,
    account_kind: ACCOUNT_KIND_OVERRIDES[accountCode] || 'cash',
  });
}

function mergeAccountRows(rows, accountCode, accountKind = 'group') {
  const merged = normalizeAccountRow({
    account_code: accountCode,
    account_kind: accountKind,
  });
  rows.forEach((row) => {
    const normalized = normalizeAccountRow(row);
    ACCOUNT_NUMERIC_FIELDS.forEach((field) => {
      merged[field] += normalized[field];
    });
    merged.latest_sync_at = maxSyncAt(merged.latest_sync_at, normalized.latest_sync_at);
  });
  return merged;
}

function hasAccountActivity(row) {
  return num(row.row_count) > 0
    || num(row.pending_count) > 0
    || hasAccountBalance(row.account_code)
    || ACCOUNT_NUMERIC_FIELDS.some((field) => field !== 'activity' && Math.abs(num(row[field])) > 0.000001);
}

function buildAccountFlowGroups(rows) {
  const rowByAccount = new Map((rows || []).map((row) => [row.account_code, normalizeAccountRow(row)]));
  const groups = ACCOUNT_GROUPS.map((group) => {
    const children = group.children.map((accountCode) => rowByAccount.get(accountCode) || emptyAccountRow(accountCode));
    const activeChildren = children.filter(hasAccountActivity);
    const summary = mergeAccountRows(children, group.code, 'group');
    return {
      ...group,
      children,
      summary,
      visible: activeChildren.length > 0 || group.forceChildren || group.code === 'benefits_root',
    };
  }).filter((group) => group.visible);

  const leftoverRows = (rows || [])
    .filter((row) => row.account_code && !ACCOUNT_GROUP_CHILD_CODES.has(row.account_code))
    .map((row) => ({
      code: `${row.account_code}_root`,
      label: accountDisplayName(row.account_code),
      children: [normalizeAccountRow(row)],
      summary: normalizeAccountRow(row),
      standalone: true,
      visible: true,
    }));

  return [...groups, ...leftoverRows];
}

function accountFlowStatsEntries(row) {
  const normalized = normalizeAccountRow(row);
  if (normalized.account_kind === 'investment') {
    return investmentAccountStatsEntries(row).map((entry) => [entry.label, entry.value]);
  }
  const entries = [
    ['收入', normalized.income],
    ['消费', normalized.expense],
    ['转账', normalized.transfer],
  ];
  if (normalized.account_kind === 'group' && Math.abs(normalized.investment_inflow) > 0.000001) {
    entries.push(['入金', normalized.investment_inflow]);
  }
  return entries.filter(([, value]) => Math.abs(num(value)) > 0.000001);
}

function formatSyncDate(value) {
  if (!value) return '--';
  const raw = String(value);
  return raw.length >= 10 ? raw.slice(5, 10) : raw;
}

function accountFlowCountText(row, childCountText = '') {
  if (childCountText) return childCountText;
  const pendingText = row.pending_count ? ` · ${row.pending_count} 待确认` : '';
  return `${row.row_count || 0} 笔${pendingText}`;
}

function currentAccountSummary(data) {
  return data.account_summary_ytd || data.account_summary || [];
}

function investmentTransferAmount(rows) {
  return (rows || [])
    .filter((row) => String(row.match_type || '').includes('investment'))
    .reduce((sum, row) => sum + num(row.amount), 0);
}

function isInvestmentTransferRow(row) {
  const cashflowType = String(row.cashflow_type || '').toLowerCase();
  const flowType = String(row.flow_type || '').toLowerCase();
  return cashflowType === 'investment_funding'
    || cashflowType === 'investment_withdrawal'
    || flowType.startsWith('investment_');
}

function isInternalTransferRow(row) {
  return row.flow_type === 'transfer' && !isInvestmentTransferRow(row);
}

function overviewPieRows(rows) {
  const validRows = (rows || []).filter((row) => num(row.amount) > 0);
  const visible = validRows.slice(0, 6);
  const restAmount = validRows.slice(6).reduce((sum, row) => sum + num(row.amount), 0);
  const restCount = validRows.slice(6).reduce((sum, row) => sum + num(row.count), 0);
  if (restAmount > 0) {
    visible.push({
      category: '其他',
      amount: restAmount,
      count: restCount,
      share: restAmount / validRows.reduce((sum, row) => sum + num(row.amount), 0),
    });
  }
  return visible;
}

function findLedgerRowById(rowId) {
  const idText = String(rowId || '');
  const groups = [
    state.data?.review_rows || [],
    state.data?.recent_rows_ytd || [],
    state.data?.recent_rows_month || [],
    state.data?.recent_rows || [],
  ];
  return groups.flat().find((row) => String(row.id || '') === idText) || null;
}

function filteredRecentRows() {
  const flow = state.recentFlowFilter || '';
  const search = String(state.recentSearch || '').trim().toLowerCase();
  const rows = state.recentPeriod === 'ytd'
    ? (state.data?.recent_rows_ytd || state.data?.recent_rows || [])
    : (state.data?.recent_rows_month || state.data?.recent_rows || []);
  const filtered = rows.filter((row) => {
    if (state.recentAccountFilter && row.account_code !== state.recentAccountFilter) return false;
    if (flow === 'review' && !row.needs_review) return false;
    if (flow === 'investment_transfer' && !isInvestmentTransferRow(row)) return false;
    if (flow === 'internal_transfer' && !isInternalTransferRow(row)) return false;
    if (flow && !['review', 'investment_transfer', 'internal_transfer'].includes(flow) && row.flow_type !== flow) return false;
    if (search && !rowSearchText(row).includes(search)) {
      return false;
    }
    return true;
  });
  return sortRecentRows(filtered);
}

  return { flowLabel, flowClass, compactText, stripLongDetail, accountFriendlyName, accountShortLabel, counterpartyShortLabel, rowCategoryText, cashflowTypeText, isEditableLedgerRow, ledgerEditType, ledgerEditTypeLabel, ledgerEditActionLabel, ledgerEditSubmitLabel, isAccountValidationRow, isInvestmentFundingRow, isInvestmentWithdrawalRow, routeEndpointLabel, rowTagLabel, rowAmountClass, rowTitle, rowMetaText, rowDateText, rowSearchText, recentAmountValue, sortRecentRows, originalCurrencyText, inferInvestmentAccountCode, transferEndpointName, isImporterCounterparty, investmentFundingSourceAccount, transferRoute, uniqueOptionValues, ledgerCategoryOptions, ledgerSubcategoryOptions, overviewPeriodKey, overviewPeriodLabel, currentOverviewPeriod, currentOverviewTransferSummary, overviewDisposableNet, overviewInvestmentTransfer, overviewInvestmentTransferNet, overviewOtherTransfer, overviewCashNet, currentOverviewCategories, overviewFlowKey, overviewFlowLabel, accountDisplayName, subAccountDisplayName, accountOverviewMap, accountOverview, accountPrimaryBalance, hasAccountBalance, accountBalanceItems, formatBalanceItem, accountBalanceSummary, accountPlatformBalances, platformBalanceAmount, addCurrencyAmount, sortedCurrencyItems, formatCurrencyItems, formatCurrencyItemsCompact, accountInvestmentBalanceEntries, investmentAccountStatsEntries, normalizeAccountRow, maxSyncAt, emptyAccountRow, mergeAccountRows, hasAccountActivity, buildAccountFlowGroups, accountFlowStatsEntries, formatSyncDate, accountFlowCountText, currentAccountSummary, investmentTransferAmount, isInvestmentTransferRow, isInternalTransferRow, overviewPieRows, findLedgerRowById, filteredRecentRows };
}

export { ACCOUNT_GROUPS, ACCOUNT_GROUP_CHILD_CODES, ACCOUNT_SUBACCOUNT_LABELS, TRANSFER_ENDPOINT_LABELS, ACCOUNT_KIND_OVERRIDES, ACCOUNT_NUMERIC_FIELDS, LEDGER_EDIT_TYPES, LEDGER_EDIT_DEFAULTS, LEDGER_EDIT_CATEGORY_OPTIONS, LEDGER_EDIT_SUBCATEGORY_OPTIONS };
