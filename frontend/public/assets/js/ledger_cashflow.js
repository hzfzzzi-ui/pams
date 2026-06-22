import { accountLabel, jget, jpost } from './api.js?v=20260606-refresh-v3';
import { closeBottomSheet, openBottomSheet, setActiveNav } from './ui.js?v=20260621-ledger-investment-account-v2';
import {
  byId as $,
  clearError,
  escapeHtml,
  formatCurrency,
  formatMoneyAxis as axisMoney,
  formatMoneyFixed2 as money,
  formatPercent,
  formatSignedMoneyFixed2 as signedMoney,
  setText,
  showError,
  toFiniteNumber as num,
} from './utils.js?v=20260607-shared-format-v1';

const state = {
  data: null,
  accounts: null,
  trendMode: 'monthly',
  activePanel: 'overview',
  overviewPeriod: 'ytd',
  overviewFlow: 'income',
  recentFlowFilter: '',
  recentAccountFilter: '',
  recentPeriod: 'ytd',
  recentSearch: '',
  recentAmountSort: '',
  expandedAccountGroups: new Set(),
};

const ledgerEditBoundButtons = new WeakSet();

const CATEGORY_PIE_COLORS = {
  income: ['#3fb950', '#58a6ff', '#d29922', '#a371f7', '#2ea043', '#79c0ff', '#8b949e'],
  expense: ['#f85149', '#d29922', '#ff7b72', '#a371f7', '#f0883e', '#db6d28', '#8b949e'],
};

const LEDGER_PANEL_HASH = {
  overview: '#cashflow-workbench',
  recent: '#recent-flow-section',
  account: '#account-cashflow-section',
};

const LEDGER_HASH_PANEL = {
  ...Object.fromEntries(Object.entries(LEDGER_PANEL_HASH).map(([panel, hash]) => [hash, panel])),
  '#income-section': 'recent',
  '#accountIssueStrip': 'account',
  '#transfers': 'recent',
  '#review-section': 'account',
};

const RECENT_SORT_OPTIONS = [
  { value: '', label: '默认排序', hint: '按流水日期显示' },
  { value: 'amount_desc', label: '金额从大到小', hint: '优先查看大额收入、消费和转账' },
  { value: 'amount_asc', label: '金额从小到大', hint: '优先查看小额零散流水' },
];

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

function renderRecentSearchButton() {
  const btn = $('recentSearchToggle');
  if (!btn) return;
  const hasSearch = Boolean(state.recentSearch);
  btn.classList.toggle('active', hasSearch);
  btn.setAttribute('aria-pressed', String(hasSearch));
  btn.title = hasSearch ? `搜索：${state.recentSearch}` : '搜索流水';
}

function recentSearchSheetHtml() {
  return `
    <form class="ledgerSearchSheet" data-recent-search-form>
      <input class="formInput" type="search" data-recent-search-input placeholder="搜索摘要 / 商户 / 粗分类" value="${escapeHtml(state.recentSearch)}" />
      <div class="ledgerSearchSheetActions">
        <button type="button" class="ledgerActionBtn ghost" data-recent-search-clear>清除</button>
        <button type="submit" class="ledgerActionBtn confirm">搜索</button>
      </div>
    </form>
  `;
}

function openRecentSearchSheet() {
  openBottomSheet('搜索流水', recentSearchSheetHtml());
  const sheet = document.querySelector('#appBottomSheet');
  const form = sheet?.querySelector('[data-recent-search-form]');
  const input = sheet?.querySelector('[data-recent-search-input]');
  input?.focus();
  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    state.recentSearch = String(input?.value || '').trim();
    renderRecentSearchButton();
    renderRecentRows();
    closeBottomSheet();
  });
  sheet?.querySelector('[data-recent-search-clear]')?.addEventListener('click', () => {
    state.recentSearch = '';
    renderRecentSearchButton();
    renderRecentRows();
    closeBottomSheet();
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

function transferRouteHtml(row) {
  const route = transferRoute(row);
  if (!route) return '';
  return `
    <div class="transferRoute rowDetailFull">
      <div class="transferRouteHeader">${escapeHtml(route.label)}</div>
      <div class="transferRouteNode">
        <em>转出账户</em>
        <strong>${escapeHtml(transferEndpointName(route.from))}</strong>
      </div>
      <div class="transferRouteArrow" aria-hidden="true">→</div>
      <div class="transferRouteNode is-target">
        <em>转入账户</em>
        <strong>${escapeHtml(transferEndpointName(route.to))}</strong>
      </div>
    </div>
  `;
}

function rowDetailHtml(row) {
  const originalAmount = originalCurrencyText(row);
  const editButton = isEditableLedgerRow(row)
    ? `
      <div class="ledgerDetailActions rowDetailFull">
        <button type="button" class="ledgerActionBtn confirm" data-ledger-edit="${escapeHtml(String(row.id || ''))}">${escapeHtml(ledgerEditActionLabel(row))}</button>
      </div>
    `
    : '';
  return `
    <div class="rowDetailGrid">
      ${transferRouteHtml(row)}
      ${editButton}
      <div><strong>账户</strong><span>${escapeHtml(accountLabel(row.account_code) || row.account_code || '-')}</span></div>
      <div><strong>对方账户</strong><span>${escapeHtml(row.counter_account_code || '不适用')}</span></div>
      <div><strong>对方</strong><span>${escapeHtml(row.counterparty || '不适用')}</span></div>
      <div><strong>原始类型</strong><span>${escapeHtml(row.flow_type || '-')}</span></div>
      ${originalAmount ? `<div><strong>原币金额</strong><span>${escapeHtml(originalAmount)}</span></div>` : ''}
      <div><strong>粗分类</strong><span>${escapeHtml(row.coarse_category || '-')}</span></div>
      <div><strong>摘要</strong><span>${escapeHtml(row.summary || '-')}</span></div>
      <div><strong>说明</strong><span>${escapeHtml(row.description || '不适用')}</span></div>
      <div><strong>状态</strong><span>${row.needs_review ? '待确认' : '已归类'}</span></div>
    </div>
  `;
}

function uniqueOptionValues(values) {
  return [...new Set(values.map((value) => String(value || '').trim()).filter(Boolean))];
}

function optionHtml(options, selectedValue) {
  const selectedText = String(selectedValue || '').trim();
  return options.map((value) => `
    <option value="${escapeHtml(value)}" ${value === selectedText ? 'selected' : ''}>${escapeHtml(value)}</option>
  `).join('');
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

function ledgerEditSheetHtml(row) {
  const amount = num(row.amount_cny ?? row.amount);
  const account = accountLabel(row.account_code) || row.account_code || '-';
  const cashflowType = ledgerEditType(row);
  const defaultEdit = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  const rawCategory = String(row.category || '').trim();
  const rawSubcategory = String(row.subcategory || '').trim();
  const category = rawCategory && rawCategory !== '未分类' ? rawCategory : (defaultEdit.category || '');
  const subcategory = rawSubcategory || defaultEdit.subcategory || '';
  return `
    <form class="ledgerConfirmSheet ledgerEditSheet" data-ledger-edit-form="${escapeHtml(String(row.id || ''))}">
      <div class="ledgerConfirmLead">${row.needs_review ? '选择正确口径后保存，这条流水会从待确认中移除。' : '修改会写入底层现金流事实表，并刷新收支和账户统计。'}</div>
      <div class="ledgerConfirmTitle">${escapeHtml(rowTitle(row))}</div>
      <div class="ledgerConfirmAmount ${amount >= 0 ? 'up' : 'down'}">${signedMoney(amount)}</div>
      <div class="ledgerConfirmMeta">
        <div>
          <span>日期 / 账户</span>
          <strong>${escapeHtml(row.transaction_date || '--')} / ${escapeHtml(account)}</strong>
        </div>
        <div>
          <span>当前口径</span>
          <strong>${escapeHtml(ledgerEditTypeLabel(cashflowType))} / ${escapeHtml(row.coarse_category || '-')}</strong>
        </div>
        <div>
          <span>对方</span>
          <strong>${escapeHtml(row.counterparty || row.summary || '未提供')}</strong>
        </div>
        <div>
          <span>说明</span>
          <strong>${escapeHtml(row.description || row.summary || '无')}</strong>
        </div>
      </div>
      <div class="ledgerEditGrid">
        <label>
          <span>流水口径</span>
          <select class="formInput" data-edit-field="cashflow_type">
            ${LEDGER_EDIT_TYPES.map(([value, label]) => `
              <option value="${escapeHtml(value)}" ${value === cashflowType ? 'selected' : ''}>${escapeHtml(label)}</option>
            `).join('')}
          </select>
        </label>
        <label>
          <span>粗分类</span>
          <select class="formInput" data-edit-field="category">
            ${optionHtml(ledgerCategoryOptions(cashflowType, category), category)}
          </select>
        </label>
        <label>
          <span>细分类</span>
          <select class="formInput" data-edit-field="subcategory">
            ${optionHtml(ledgerSubcategoryOptions(category, subcategory), subcategory)}
          </select>
        </label>
        <label>
          <span>对方账户标识</span>
          <input class="formInput" data-edit-field="counter_account_code" value="${escapeHtml(row.counter_account_code || '')}" placeholder="内部转账 / 入金可填" />
        </label>
        <label class="ledgerEditFull">
          <span>对方 / 商户</span>
          <input class="formInput" data-edit-field="counterparty" value="${escapeHtml(row.counterparty || '')}" />
        </label>
      </div>
      <div class="ledgerConfirmActions">
        <button type="button" class="ledgerActionBtn ghost" data-sheet-close>取消</button>
        <button type="submit" class="ledgerActionBtn confirm">${escapeHtml(ledgerEditSubmitLabel(row))}</button>
      </div>
    </form>
  `;
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

function renderOverviewPeriodButtons() {
  document.querySelectorAll('[data-overview-period]').forEach((btn) => {
    btn.classList.toggle('active', (btn.dataset.overviewPeriod || 'ytd') === overviewPeriodKey());
  });
}

function renderOverviewFlowButtons() {
  document.querySelectorAll('[data-overview-flow]').forEach((btn) => {
    btn.classList.toggle('active', (btn.dataset.overviewFlow || 'income') === overviewFlowKey());
  });
}

function renderKpis(data) {
  const period = currentOverviewPeriod(data);
  const investmentTransfer = overviewInvestmentTransfer(data, period);
  const internalTransfer = overviewOtherTransfer(data, period);
  const disposableNet = overviewDisposableNet(period);
  setText('kpiMonthIncome', money(period.income || 0));
  setText('kpiMonthExpense', money(period.expense || 0));
  setText('kpiDisposableNet', signedMoney(disposableNet));
  setText('kpiInvestmentTransfer', money(investmentTransfer || 0));
  setText('kpiTransfer', money(internalTransfer || 0));
  setText('kpiPending', String(period.pending_count || 0));
  const netEl = $('kpiDisposableNet');
  if (netEl) {
    netEl.classList.toggle('up', disposableNet >= 0);
    netEl.classList.toggle('down', disposableNet < 0);
  }
  const pendingEl = $('kpiPending');
  if (pendingEl) pendingEl.classList.toggle('down', num(period.pending_count) > 0);
}

function renderLedgerMobileHero(data) {
  const el = $('ledgerMobileHero');
  if (!el) return;
  const period = currentOverviewPeriod(data);
  const disposableNet = overviewDisposableNet(period);
  const pending = num(period.pending_count || 0);
  el.innerHTML = `
    <div class="ledgerMobileLabel">${overviewPeriodLabel()}可支配结余</div>
    <div class="ledgerMobileValue ${disposableNet >= 0 ? 'up' : 'down'}">${signedMoney(disposableNet)}</div>
    <div class="ledgerMobileStats">
      <div class="ledgerMobileStat">
        <span>收入</span>
        <strong>${money(period.income || 0)}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>消费</span>
        <strong class="down">${signedMoney(-Math.abs(num(period.expense || 0)))}</strong>
      </div>
      <div class="ledgerMobileStat">
        <span>待确认</span>
        <strong class="${pending ? 'down' : ''}">${pending}</strong>
      </div>
    </div>
  `;
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

function accountFlowStatsHtml(row) {
  const entries = accountFlowStatsEntries(row);
  if (!entries.length) {
    return '<span>暂无流水</span>';
  }
  return entries.map(([label, value]) => {
    const formatted = typeof value === 'string' ? value : money(value);
    return `<span>${escapeHtml(label)} ${escapeHtml(formatted)}</span>`;
  }).join('');
}

function formatSyncDate(value) {
  if (!value) return '--';
  const raw = String(value);
  return raw.length >= 10 ? raw.slice(5, 10) : raw;
}

function syncBadgeHtml(row) {
  return `<span class="accountSyncBadge">同步 ${escapeHtml(formatSyncDate(row.latest_sync_at))}</span>`;
}

function accountPendingBadgeHtml(row) {
  const pending = Number(row?.pending_count || 0);
  if (!pending) return '';
  return `<span class="accountPendingBadge">${pending} 笔待确认</span>`;
}

function accountFlowCountText(row, childCountText = '') {
  if (childCountText) return childCountText;
  const pendingText = row.pending_count ? ` · ${row.pending_count} 待确认` : '';
  return `${row.row_count || 0} 笔${pendingText}`;
}

function renderAccountFlowSide(row, balanceRows = null) {
  const rowsForBalance = Array.isArray(balanceRows) && balanceRows.length ? balanceRows : [row];
  const balance = accountBalanceSummary(rowsForBalance);
  return `
    <span class="accountFlowSide">
      <b class="accountBalanceValue ${balance.tone}">${escapeHtml(balance.value)}</b>
      <small>${escapeHtml(balance.meta)}</small>
    </span>
  `;
}

function renderAccountFlowFooter(row, childCountText = '', { showStats = true } = {}) {
  const net = num(row.net);
  const deltaTone = net > 0 ? 'up' : net < 0 ? 'down' : '';
  const stats = showStats ? accountFlowStatsHtml(row) : '';
  return `
    <span class="accountFlowFooter ${stats ? '' : 'is-compact'}">
      ${stats ? `<span class="accountFlowStats">${stats}</span>` : ''}
      <span class="accountFlowFooterMeta">
        <span class="accountFlowNetBadge ${deltaTone}">年初净流入 ${escapeHtml(signedMoney(net))}</span>
        <span>${escapeHtml(accountFlowCountText(row, childCountText))}</span>
      </span>
    </span>
  `;
}

function investmentAccountChipHtml(entry) {
  return `
    <span class="accountInvestmentChip ${entry.tone || ''}">
      <span>${escapeHtml(entry.label)}</span>
      <strong>${escapeHtml(entry.value)}</strong>
    </span>
  `;
}

function renderInvestmentAccountFooter(row, childCountText = '') {
  const normalized = normalizeAccountRow(row);
  const flowEntries = investmentAccountStatsEntries(normalized);
  const balanceEntries = accountInvestmentBalanceEntries(normalized.account_code);
  const entries = [...flowEntries, ...balanceEntries];
  const content = entries.length
    ? entries.map(investmentAccountChipHtml).join('')
    : '<span class="accountInvestmentChip"><span>资金流</span><strong>暂无入金记录</strong></span>';
  return `
    <span class="accountFlowFooter accountInvestmentFooter">
      <span class="accountInvestmentStats">${content}</span>
      <span class="accountFlowFooterMeta">
        <span>${escapeHtml(accountFlowCountText(normalized, childCountText))}</span>
      </span>
    </span>
  `;
}

function renderAccountFlowRow(row, { label, detail, classes = '', filterable = true } = {}) {
  const normalized = normalizeAccountRow(row);
  const active = hasAccountActivity(row);
  const tagName = filterable && active ? 'button' : 'div';
  const typeAttr = tagName === 'button' ? ' type="button"' : '';
  const filterAttr = tagName === 'button' ? ` data-account-flow-filter="${escapeHtml(normalized.account_code || '')}"` : '';
  const rowClasses = [
    classes,
    normalized.account_kind === 'investment' ? 'accountInvestmentRow' : '',
    active ? '' : 'is-empty',
  ].filter(Boolean).join(' ');
  return `
    <${tagName}${typeAttr} class="accountFlowRow ${rowClasses}"${filterAttr}>
      <span class="accountFlowHeader">
        <span class="accountFlowMain">
          <strong>${escapeHtml(label || accountDisplayName(normalized.account_code))}</strong>
          <small class="accountFlowMetaLine"><span>${escapeHtml(detail || normalized.account_code || '')}</span>${syncBadgeHtml(normalized)}${accountPendingBadgeHtml(normalized)}</small>
        </span>
        ${renderAccountFlowSide(normalized)}
      </span>
      ${normalized.account_kind === 'investment' ? renderInvestmentAccountFooter(normalized) : renderAccountFlowFooter(normalized)}
    </${tagName}>
  `;
}

function renderAccountGroup(group) {
  const summary = group.summary;
  const childRows = group.children || [];
  const shouldShowChildren = group.forceChildren || childRows.length > 1;
  if (group.standalone && !shouldShowChildren) {
    const row = childRows[0] || summary;
    return renderAccountFlowRow(row, {
      label: group.label,
      detail: row.account_code,
      classes: 'accountStandaloneRow',
    });
  }

  const collapsed = shouldShowChildren && !state.expandedAccountGroups.has(group.code);
  const activeChildren = childRows.filter(hasAccountActivity).length;
  const childCountText = `${activeChildren}/${childRows.length} 子 · ${summary.row_count || 0} 笔${summary.pending_count ? ` · ${summary.pending_count} 待确认` : ''}`;
  return `
    <div class="accountGroupBlock" data-account-group="${escapeHtml(group.code)}">
      <button type="button" class="accountFlowRow accountGroupRow" data-account-group-toggle="${escapeHtml(group.code)}" aria-expanded="${collapsed ? 'false' : 'true'}">
        <span class="accountFlowHeader">
          <span class="accountFlowMain">
            <strong>${escapeHtml(group.label)}</strong>
            <small class="accountFlowMetaLine">${syncBadgeHtml(summary)}${accountPendingBadgeHtml(summary)}<span class="accountGroupToggle">${collapsed ? '展开' : '收起'}</span></small>
          </span>
          ${renderAccountFlowSide(summary, childRows)}
        </span>
        ${renderAccountFlowFooter(summary, childCountText, { showStats: false })}
      </button>
      <div class="accountSubList" ${collapsed ? 'hidden' : ''}>
        ${childRows.map((row) => renderAccountFlowRow(row, {
          label: subAccountDisplayName(row.account_code),
          detail: row.account_code,
          classes: 'accountSubRow',
        })).join('')}
      </div>
    </div>
  `;
}

function renderRecentAccountScope() {
  const el = $('recentAccountScope');
  if (!el) return;
  if (!state.recentAccountFilter) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  const periodLabel = state.recentPeriod === 'ytd' ? '年初至今' : '本月';
  el.innerHTML = `
    <span>账户筛选 <strong>${escapeHtml(accountDisplayName(state.recentAccountFilter))}</strong><small> · ${escapeHtml(periodLabel)}</small></span>
    <button type="button" data-clear-account-filter>清除</button>
  `;
  el.querySelector('[data-clear-account-filter]')?.addEventListener('click', () => {
    state.recentAccountFilter = '';
    renderRecentAccountScope();
    renderRows('recentRows', filteredRecentRows(), 120);
  });
}

function openRecentForAccount(accountCode) {
  state.recentAccountFilter = accountCode || '';
  state.recentFlowFilter = '';
  state.recentPeriod = 'ytd';
  renderRecentPeriodButtons();
  document.querySelectorAll('[data-ledger-flow-filter]').forEach((item) => {
    item.classList.toggle('active', (item.dataset.ledgerFlowFilter || '') === '');
  });
  renderRecentAccountScope();
  renderRows('recentRows', filteredRecentRows(), 120);
  history.replaceState(null, '', `${window.location.pathname}${window.location.search}${LEDGER_PANEL_HASH.recent}`);
  setLedgerPanel('recent');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderRecentPeriodButtons() {
  document.querySelectorAll('[data-recent-period]').forEach((btn) => {
    btn.classList.toggle('active', (btn.dataset.recentPeriod || 'month') === state.recentPeriod);
  });
}

function renderRecentSortButton() {
  const btn = $('recentSortToggle');
  if (!btn) return;
  const hasSort = Boolean(state.recentAmountSort);
  const label = RECENT_SORT_OPTIONS.find((item) => item.value === state.recentAmountSort)?.label || '流水排序';
  btn.classList.toggle('active', hasSort);
  btn.setAttribute('aria-pressed', String(hasSort));
  btn.title = label;
}

function recentSortSheetHtml() {
  return `
    <div class="ledgerOptionSheet">
      ${RECENT_SORT_OPTIONS.map((option) => `
        <button type="button" class="ledgerOptionButton ${option.value === state.recentAmountSort ? 'active' : ''}" data-recent-sort-option="${escapeHtml(option.value)}">
          <span>
            <strong>${escapeHtml(option.label)}</strong>
            <small>${escapeHtml(option.hint)}</small>
          </span>
          ${option.value === state.recentAmountSort ? '<em>✓</em>' : ''}
        </button>
      `).join('')}
    </div>
  `;
}

function openRecentSortSheet() {
  openBottomSheet('流水排序', recentSortSheetHtml());
}

function renderAccountFlowSummary(rows) {
  const el = $('accountFlowRows');
  if (!el) return;
  const hint = $('accountFlowHint');
  if (hint) {
    hint.textContent = '账户余额优先展示；现金流变化统一按年初至今计算。';
  }
  if (!rows?.length) {
    el.innerHTML = '<div class="ledgerEmpty">年初至今暂无账户流水</div>';
    return;
  }
  el.innerHTML = buildAccountFlowGroups(rows).map(renderAccountGroup).join('');
  el.querySelectorAll('[data-account-flow-filter]').forEach((btn) => {
    btn.addEventListener('click', () => openRecentForAccount(btn.dataset.accountFlowFilter || ''));
  });
  el.querySelectorAll('[data-account-group-toggle]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const groupCode = btn.dataset.accountGroupToggle || '';
      if (state.expandedAccountGroups.has(groupCode)) {
        state.expandedAccountGroups.delete(groupCode);
      } else {
        state.expandedAccountGroups.add(groupCode);
      }
      renderAccountFlowSummary(rows);
    });
  });
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

function renderCategoryBars(id, rows, emptyText, flowType = 'income') {
  const el = $(id);
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = `<div class="muted">${escapeHtml(emptyText)}</div>`;
    return;
  }
  el.innerHTML = `
    <div class="categoryBarList ${flowType === 'expense' ? 'expense' : 'income'}">
      ${rows.map((row, index) => `
        <div class="categoryBarRow">
          <div class="categoryBarRank">${index + 1}</div>
          <div class="categoryBarMain">
            <div class="categoryBarName">${escapeHtml(row.category)}</div>
            <div class="categoryTrack"><div class="categoryFill" style="width:${Math.max(num(row.share) * 100, 2)}%"></div></div>
            <div class="categoryBarCount">${row.count || 0} 笔</div>
          </div>
          <div class="categoryBarMeta">
            <b>${money(row.amount)}</b>
            <span>${formatPercent(row.share, 1)}</span>
          </div>
        </div>
      `).join('')}
    </div>
  `;
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

function renderOverviewCategoryPieFallback(canvas, chartRows, flowType) {
  const wrap = canvas?.parentElement;
  if (!wrap) return;

  wrap.querySelector('.categoryPieFallback')?.remove();
  const total = chartRows.reduce((sum, row) => sum + num(row.amount), 0);
  if (!total) return;

  const colors = CATEGORY_PIE_COLORS[flowType] || CATEGORY_PIE_COLORS.income;
  let cursor = 0;
  const slices = chartRows.map((row, index) => {
    const start = cursor;
    cursor += (num(row.amount) / total) * 100;
    return `${colors[index % colors.length]} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`;
  }).join(', ');

  const fallback = document.createElement('div');
  fallback.className = 'categoryPieFallback';
  fallback.style.background = `conic-gradient(${slices})`;
  fallback.setAttribute(
    'aria-label',
    chartRows.map((row) => `${row.category} ${formatPercent(num(row.amount) / total, 1)}`).join('，'),
  );
  wrap.insertBefore(fallback, canvas);
  canvas.hidden = true;
}

function renderOverviewCategoryPie(rows, flowType) {
  const canvas = $('overviewCategoryPieChart');
  const empty = $('overviewCategoryPieEmpty');
  if (window.__overviewCategoryPieChart) {
    window.__overviewCategoryPieChart.destroy();
    window.__overviewCategoryPieChart = null;
  }
  canvas?.parentElement?.querySelector('.categoryPieFallback')?.remove();
  if (!canvas) return;

  const chartRows = overviewPieRows(rows);
  const hasRows = chartRows.length > 0;
  canvas.hidden = !hasRows;
  if (empty) {
    empty.hidden = hasRows;
    empty.textContent = hasRows ? '' : `暂无${overviewFlowLabel(flowType)}分类`;
  }
  if (!hasRows) return;
  if (!window.Chart) {
    renderOverviewCategoryPieFallback(canvas, chartRows, flowType);
    return;
  }
  canvas.hidden = false;

  const colors = CATEGORY_PIE_COLORS[flowType] || CATEGORY_PIE_COLORS.income;
  window.__overviewCategoryPieChart = new Chart(canvas, {
    type: 'pie',
    data: {
      labels: chartRows.map((row) => row.category),
      datasets: [{
        data: chartRows.map((row) => num(row.amount)),
        backgroundColor: chartRows.map((_, index) => colors[index % colors.length]),
        borderColor: 'rgba(5,7,10,.92)',
        borderWidth: 2,
        hoverOffset: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctxItem) => {
              const value = num(ctxItem.parsed);
              const total = chartRows.reduce((sum, row) => sum + num(row.amount), 0);
              const share = total ? value / total : 0;
              return `${ctxItem.label}: ${money(value)} / ${formatPercent(share, 1)}`;
            },
          },
        },
      },
    },
  });
}

function renderOverviewCategoryInsight(data) {
  const flowType = overviewFlowKey();
  const rows = currentOverviewCategories(data, flowType);
  setText('categoryInsightTitle', `${overviewFlowLabel(flowType)}排行`);
  renderOverviewFlowButtons();
  renderCategoryBars(
    'overviewCategoryRank',
    rows,
    `${overviewPeriodLabel()}暂无${overviewFlowLabel(flowType)}流水。`,
    flowType,
  );
  renderOverviewCategoryPie(rows, flowType);
}

function renderCashflowBridge(data) {
  const el = $('cashflowBridge');
  if (!el) return;
  const period = currentOverviewPeriod(data);
  const investmentMovement = overviewInvestmentTransfer(data, period);
  const otherTransfer = overviewOtherTransfer(data, period);
  const internalMovement = num(period.internal_transfer ?? (investmentMovement + otherTransfer));
  const cashNet = overviewCashNet(data, period);
  const fundingItems = [
    ['内部资金流动', money(internalMovement), 'data'],
    ['投资账户流动', money(investmentMovement), 'data'],
    ['现金净变化', signedMoney(cashNet), cashNet >= 0 ? 'good' : 'bad'],
  ];
  el.innerHTML = `
    <div class="cashflowBridgeLine">
      <div class="cashflowBridgeHead">
        <span>资金调拨</span>
        <strong>NAV 内部迁移，不计入消费</strong>
      </div>
      <div class="cashflowTransferGrid">
        ${fundingItems.map(([label, value, tone]) => `
          <div class="cashflowTransferStat ${tone}">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
          </div>
        `).join('')}
      </div>
    </div>
  `;
}

function renderTransferSummary(rows) {
  const el = $('transferSummary');
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = '<div class="muted">本期未识别到资金流转匹配。</div>';
    return;
  }
  el.innerHTML = rows.slice(0, 10).map((row) => `
    <div class="analysisRow">
      <div>
        <div class="analysisLabel">${escapeHtml(row.relation_code)}</div>
        <div class="analysisSub">${escapeHtml(row.match_type)} / ${row.count || 0} 笔</div>
      </div>
      <div class="analysisValue">${money(row.amount)}</div>
    </div>
  `).join('');
}

function renderRules(rows) {
  const el = $('classificationRules');
  if (!el) return;
  el.innerHTML = (rows || []).map((rule) => `
    <div class="ruleItem">
      <div class="ruleName">${escapeHtml(rule.name)}</div>
      <div class="muted">${escapeHtml(rule.description)}</div>
    </div>
  `).join('');
}

function renderTrendChart(data) {
  const rows = state.trendMode === 'weekly' ? data.weekly_trend || [] : data.monthly_trend || [];
  const ctx = $('cashflowTrendChart');
  if (!ctx || !window.Chart) return;
  if (window.__cashflowTrendChart) window.__cashflowTrendChart.destroy();
  window.__cashflowTrendChart = new Chart(ctx, {
    data: {
      labels: rows.map((r) => r.period),
      datasets: [
        {
          type: 'bar',
          label: '收入',
          data: rows.map((r) => num(r.income)),
          backgroundColor: 'rgba(63,185,80,.42)',
          borderColor: 'rgba(63,185,80,.8)',
          borderWidth: 1,
        },
        {
          type: 'bar',
          label: '消费',
          data: rows.map((r) => num(r.expense)),
          backgroundColor: 'rgba(248,81,73,.38)',
          borderColor: 'rgba(248,81,73,.82)',
          borderWidth: 1,
        },
        {
          type: 'line',
          label: '净流入',
          data: rows.map((r) => num(r.net)),
          borderColor: '#58a6ff',
          backgroundColor: 'rgba(88,166,255,.12)',
          tension: 0.25,
          pointRadius: 2,
          yAxisID: 'net',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { left: 8, right: 18, top: 2, bottom: 4 } },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#8b949e', boxWidth: 28, boxHeight: 10, padding: 12 } },
        tooltip: {
          callbacks: {
            label: (ctxItem) => `${ctxItem.dataset.label}: ${money(ctxItem.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: '#8b949e' }, grid: { color: 'rgba(139,148,158,.12)' } },
        y: { ticks: { color: '#8b949e', callback: axisMoney }, grid: { color: 'rgba(139,148,158,.14)' } },
        net: { position: 'right', ticks: { color: '#8b949e', callback: axisMoney }, grid: { drawOnChartArea: false } },
      },
    },
  });
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

function updateLedgerCategoryOptions(form, preferredValue) {
  const cashflowType = form.querySelector('[data-edit-field="cashflow_type"]')?.value || '';
  const category = form.querySelector('[data-edit-field="category"]');
  if (!category) return '';
  const currentValue = String((preferredValue ?? category.value) || '').trim();
  const options = ledgerCategoryOptions(cashflowType, currentValue);
  category.innerHTML = optionHtml(options, currentValue);
  category.value = options.includes(currentValue) ? currentValue : (options[0] || '');
  return category.value;
}

function updateLedgerSubcategoryOptions(form, preferredValue) {
  const categoryValue = form.querySelector('[data-edit-field="category"]')?.value || '';
  const subcategory = form.querySelector('[data-edit-field="subcategory"]');
  if (!subcategory) return '';
  const options = ledgerSubcategoryOptions(categoryValue, preferredValue ?? subcategory.value);
  const nextValue = preferredValue === null
    ? (options[0] || '')
    : String((preferredValue ?? subcategory.value) || '').trim();
  subcategory.innerHTML = optionHtml(options, nextValue);
  subcategory.value = options.includes(nextValue) ? nextValue : (options[0] || '');
  return subcategory.value;
}

function applyLedgerEditDefaults(form) {
  const cashflowType = form.querySelector('[data-edit-field="cashflow_type"]')?.value || '';
  const defaults = LEDGER_EDIT_DEFAULTS[cashflowType] || {};
  updateLedgerCategoryOptions(form, defaults.category || undefined);
  updateLedgerSubcategoryOptions(form, defaults.subcategory || undefined);
}

function bindLedgerEditSheet(row) {
  const sheet = document.querySelector('#appBottomSheet');
  const closeBtn = document.querySelector('#appBottomSheet [data-sheet-close]');
  if (closeBtn) {
    closeBtn.addEventListener('click', (event) => {
      event.preventDefault();
      closeBottomSheet();
    }, { once: true });
  }
  const form = sheet?.querySelector('[data-ledger-edit-form]');
  if (!form) return;
  form.querySelector('[data-edit-field="cashflow_type"]')?.addEventListener('change', () => applyLedgerEditDefaults(form));
  form.querySelector('[data-edit-field="category"]')?.addEventListener('change', () => updateLedgerSubcategoryOptions(form, null));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = '保存中...';
    }
    const fieldValue = (name) => form.querySelector(`[data-edit-field="${name}"]`)?.value?.trim() || '';
    try {
      await jpost(`/api/ledger/rows/${encodeURIComponent(row.id)}/update`, {
        cashflow_type: fieldValue('cashflow_type'),
        category: fieldValue('category'),
        subcategory: fieldValue('subcategory'),
        counter_account_code: fieldValue('counter_account_code'),
        counterparty: fieldValue('counterparty'),
      });
      closeBottomSheet();
      await loadWorkbench();
    } catch (err) {
      showError(err);
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = ledgerEditSubmitLabel(row);
      }
    }
  }, { once: true });
}

function openLedgerEdit(row) {
  if (!row || !isEditableLedgerRow(row)) return;
  openBottomSheet(row.needs_review ? '确认流水' : '修改流水', ledgerEditSheetHtml(row));
  bindLedgerEditSheet(row);
}

function bindLedgerEditButtons(root = document) {
  if (!root || ledgerEditBoundButtons.has(root)) return;
  ledgerEditBoundButtons.add(root);
  root.addEventListener('click', (event) => {
    const btn = event.target?.closest?.('[data-ledger-edit]');
    if (!btn || !root.contains(btn)) return;
    event.preventDefault();
    event.stopPropagation();
    const row = findLedgerRowById(btn.dataset.ledgerEdit);
    openLedgerEdit(row);
  });
}

function renderRows(id, rows, limit = 80) {
  const el = $(id);
  if (!el) return;
  if (!rows?.length) {
    el.innerHTML = '<div class="ledgerEmpty">暂无流水</div>';
    return;
  }
  const displayRows = rows.slice(0, limit);
  el.innerHTML = displayRows.map((row, index) => {
    const rawId = row.id || row.transaction_id || `${row.transaction_date || 'row'}-${index}`;
    const detailId = `${id}-detail-${String(rawId).replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    const amount = num(row.amount_cny ?? row.amount);
    const amountClass = rowAmountClass(row, amount);
    const title = rowTitle(row);
    const meta = rowMetaText(row);
    const dateText = rowDateText(row);
    const originalAmount = originalCurrencyText(row);
    const reviewAction = row.needs_review && isEditableLedgerRow(row)
      ? `<button type="button" class="ledgerInlineEditBtn" data-ledger-edit="${escapeHtml(String(row.id || ''))}">${escapeHtml(ledgerEditActionLabel(row))}</button>`
      : '';
    return `
      <div class="ledgerListRow" role="button" tabindex="0" data-detail-target="${escapeHtml(detailId)}" title="点击查看账户、对方和原始摘要">
        <span class="ledgerListMain">
          <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
          <small class="ledgerListAccount" title="${escapeHtml(rowSearchText(row))}">${escapeHtml(meta)}</small>
          <small class="ledgerListDate">${escapeHtml(dateText)}</small>
        </span>
        <span class="ledgerListSide">
          <b class="${amountClass}">${signedMoney(amount)}</b>
          ${originalAmount ? `<small class="ledgerOriginalAmount">原币 ${escapeHtml(originalAmount)}</small>` : ''}
          <small class="ledgerListMeta">
            <span class="flowTag ${flowClass(row.flow_type)}">${escapeHtml(rowTagLabel(row))}</span>
            ${row.needs_review ? '<span class="ledgerReviewHint">待确认支出</span>' : ''}
          </small>
          ${reviewAction}
        </span>
      </div>
      <div id="${escapeHtml(detailId)}" class="ledgerInlineDetail" hidden>
        ${rowDetailHtml(row)}
      </div>
    `;
  }).join('');

  el.querySelectorAll('.ledgerListRow').forEach((rowEl) => {
    const openDetail = () => {
      const detail = document.getElementById(rowEl.dataset.detailTarget);
      if (!detail) return;
      if (window.matchMedia('(max-width: 760px)').matches) {
        const title = rowEl.querySelector('.ledgerListMain strong')?.innerText || '流水详情';
        openBottomSheet(title, detail.innerHTML);
        bindLedgerEditButtons(document.querySelector('#appBottomSheet') || document);
        return;
      }
      detail.hidden = !detail.hidden;
      rowEl.classList.toggle('open', !detail.hidden);
      if (!detail.hidden) bindLedgerEditButtons(detail);
    };
    rowEl.addEventListener('click', openDetail);
    rowEl.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDetail();
      }
    });
  });

  bindLedgerEditButtons(el);
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

function renderRecentRows() {
  const rows = filteredRecentRows();
  renderRows('recentRows', rows, rows.length);
}

function renderAll(data) {
  renderOverviewPeriodButtons();
  renderLedgerMobileHero(data);
  renderKpis(data);
  renderAccountFlowSummary(currentAccountSummary(data));
  renderOverviewCategoryInsight(data);
  renderTransferSummary(currentOverviewTransferSummary(data));
  renderRules(data.classification_rules || []);
  renderTrendChart(data);
  renderCashflowBridge(data);
  renderRecentSearchButton();
  renderRecentPeriodButtons();
  renderRecentSortButton();
  renderRecentAccountScope();
  renderRecentRows();
}

async function loadWorkbench() {
  clearError();
  const params = new URLSearchParams({ baseCurrency: 'CNY', recentDays: '30' });
  const [data, accounts] = await Promise.all([
    jget(`/api/ledger/workbench?${params.toString()}`, { cache: false }),
    jget('/api/accounts/overview').catch(() => ({ summary: {}, accounts: [] })),
  ]);
  state.data = data;
  state.accounts = accounts;
  renderAll(data);
}

function bindEvents() {
  bindLedgerAnchors();
  $('btnReloadLedger')?.addEventListener('click', loadWorkbench);
  document.querySelectorAll('[data-overview-period]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.overviewPeriod = btn.dataset.overviewPeriod || 'ytd';
      if (state.data) renderAll(state.data);
    });
  });
  document.querySelectorAll('[data-overview-flow]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.overviewFlow = btn.dataset.overviewFlow || 'income';
      if (state.data) renderOverviewCategoryInsight(state.data);
    });
  });
  $('trendMonthly')?.addEventListener('click', () => {
    state.trendMode = 'monthly';
    $('trendMonthly')?.classList.add('active');
    $('trendWeekly')?.classList.remove('active');
    if (state.data) renderTrendChart(state.data);
  });
  $('trendWeekly')?.addEventListener('click', () => {
    state.trendMode = 'weekly';
    $('trendWeekly')?.classList.add('active');
    $('trendMonthly')?.classList.remove('active');
    if (state.data) renderTrendChart(state.data);
  });
  document.querySelectorAll('[data-ledger-flow-filter]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.recentFlowFilter = btn.dataset.ledgerFlowFilter || '';
      document.querySelectorAll('[data-ledger-flow-filter]').forEach((item) => {
        item.classList.toggle('active', item === btn);
      });
      renderRecentRows();
    });
  });
  document.querySelectorAll('[data-recent-period]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.recentPeriod = btn.dataset.recentPeriod || 'month';
      renderRecentPeriodButtons();
      renderRecentAccountScope();
      renderRecentRows();
    });
  });
  const recentSearchToggle = $('recentSearchToggle');
  recentSearchToggle?.addEventListener('click', () => {
    openRecentSearchSheet();
  });
  $('recentSortToggle')?.addEventListener('click', () => {
    openRecentSortSheet();
  });
  document.addEventListener('click', (event) => {
    const sortOption = event.target?.closest?.('[data-recent-sort-option]');
    if (!sortOption) return;
    state.recentAmountSort = sortOption.dataset.recentSortOption || '';
    renderRecentSortButton();
    renderRecentRows();
    closeBottomSheet();
  });
  renderRecentSearchButton();
  renderRecentSortButton();
}

function setLedgerPanel(panel = 'overview') {
  state.activePanel = LEDGER_PANEL_HASH[panel] ? panel : 'overview';
  document.body.dataset.ledgerTab = state.activePanel;
  document.querySelectorAll('[data-ledger-panel]').forEach((group) => {
    group.hidden = group.dataset.ledgerPanel !== state.activePanel;
  });
  document.querySelectorAll('.ledgerAnchorNav a').forEach((link) => {
    link.classList.toggle('is-active', link.dataset.ledgerTab === state.activePanel);
  });
}

function bindLedgerAnchors() {
  document.querySelectorAll('.ledgerAnchorNav a').forEach((link) => {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      const panel = link.dataset.ledgerTab || 'overview';
      const nextHash = LEDGER_PANEL_HASH[panel] || LEDGER_PANEL_HASH.overview;
      if (window.location.hash !== nextHash) {
        history.replaceState(null, '', `${window.location.pathname}${window.location.search}${nextHash}`);
      }
      setLedgerPanel(panel);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });
  window.addEventListener('hashchange', () => {
    setLedgerPanel(LEDGER_HASH_PANEL[window.location.hash] || 'overview');
  });
  setLedgerPanel(LEDGER_HASH_PANEL[window.location.hash] || 'overview');
}

async function init() {
  setActiveNav();
  bindEvents();
  await loadWorkbench();
}

init().catch(showError);
