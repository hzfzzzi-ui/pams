import { accountCodesForLabel, jget, jpost } from '../../api.js';

export function readLedgerWorkbench({signal} = {}) {
  return jget('/api/ledger/workbench?view=overview', { cache: false, signal });
}

export function readLedgerStream(state, { offset, signal }) {
  const search = (state.recentSearch || '').slice(0, 200);
  const params = new URLSearchParams({
    view: 'stream', period: state.recentPeriod, account: state.recentAccountFilter || '',
    flow: state.recentFlowFilter || '', search,
    matchingAccounts: accountCodesForLabel(search).join(','),
    sort: state.recentAmountSort || '', offset,
  });
  return jget(`/api/ledger/workbench?${params}`, { cache: false, signal });
}

export function updateLedgerRow(row, fields) {
  return jpost(`/api/ledger/rows/${encodeURIComponent(row.id)}/update`, fields);
}

export function readLedgerTransfers({ month, signal }) {
  const params = new URLSearchParams({ view: 'transfers' });
  if (month) params.set('month', month);
  return jget(`/api/ledger/workbench?${params}`, { cache: false, signal });
}

export function readTransferCandidates() {
  return jpost('/api/bank-reconcile/transfer-matches/run', {});
}

export function confirmTransferPair(pair) {
  return jpost('/api/bank-reconcile/transfers/confirm', {
    from_kind: 'cashflow', from_id: pair.source_row_id,
    to_kind: 'cashflow', to_id: pair.target_row_id,
    note: '在收支内部划转页人工核对确认', confirmed: true,
  });
}
