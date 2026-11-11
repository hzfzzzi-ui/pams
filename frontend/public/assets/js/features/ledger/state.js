const LEDGER_PANEL_HASH = {
  overview: '#cashflow-workbench',
  recent: '#recent-flow-section',
  transfer: '#transfers',
};

const LEDGER_HASH_PANEL = {
  ...Object.fromEntries(Object.entries(LEDGER_PANEL_HASH).map(([panel, hash]) => [hash, panel])),
  '#income-section': 'recent',
};

const RETIRED_ACCOUNT_HASHES = new Set(['#account-cashflow-section', '#accountIssueStrip', '#review-section']);

const RECENT_SORT_OPTIONS = [
  { value: '', label: '默认排序', hint: '按流水日期显示' },
  { value: 'amount_desc', label: '金额从大到小', hint: '优先查看大额收入和消费' },
  { value: 'amount_asc', label: '金额从小到大', hint: '优先查看小额零散流水' },
];

export function createLedgerState() {
  return {
  data: null,
  accounts: null,
  trendMode: 'monthly',
  activePanel: 'overview',
  overviewPeriod: 'ytd',
  overviewFlow: 'income',
  transferPeriod: 'ytd',
  transferMonth: '',
  streamIsRemembered: false,
  recentFlowFilter: '',
  recentAccountFilter: '',
  recentAccountGroup: '',
  streamAccountGroups: [],
  recentPeriod: 'ytd',
  recentSearch: '',
  recentAmountSort: '',
};
}

export { LEDGER_PANEL_HASH, LEDGER_HASH_PANEL, RETIRED_ACCOUNT_HASHES, RECENT_SORT_OPTIONS };

const VIEW_FIELDS = ['trendMode', 'activePanel', 'overviewPeriod', 'overviewFlow', 'transferPeriod', 'transferMonth',
  'recentFlowFilter', 'recentAccountFilter', 'recentAccountGroup', 'recentPeriod', 'recentSearch', 'recentAmountSort'];

export function snapshotLedgerViewState(state) {
  return Object.fromEntries(VIEW_FIELDS.map(key => [key, state[key]]));
}

export function restoreLedgerViewState(state, snapshot) {
  if (!snapshot) return;
  for (const key of VIEW_FIELDS) if (key in snapshot) state[key] = snapshot[key];
  if (!['income', 'expense'].includes(state.recentFlowFilter)) state.recentFlowFilter = '';
}
