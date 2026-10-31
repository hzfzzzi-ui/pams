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

export function createLedgerState() {
  return {
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
}

export { LEDGER_PANEL_HASH, LEDGER_HASH_PANEL, RECENT_SORT_OPTIONS };

const VIEW_FIELDS = ['trendMode', 'activePanel', 'overviewPeriod', 'overviewFlow', 'recentFlowFilter', 'recentAccountFilter', 'recentPeriod', 'recentSearch', 'recentAmountSort'];

export function snapshotLedgerViewState(state) {
  const snapshot = Object.fromEntries(VIEW_FIELDS.map(key => [key, state[key]]));
  snapshot.expandedAccountGroups = [...state.expandedAccountGroups];
  return snapshot;
}

export function restoreLedgerViewState(state, snapshot) {
  if (!snapshot) return;
  for (const key of VIEW_FIELDS) if (key in snapshot) state[key] = snapshot[key];
  state.expandedAccountGroups = new Set(snapshot.expandedAccountGroups || []);
}
