import { pageDataInvalidator } from '../../app/page_memory.js';
import { accountCodesForLabel, jget, jpost } from '../../api.js';

export function readLedgerWorkbench({signal} = {}) {
  return jget('/api/ledger/workbench?view=overview', { cache: false, signal });
}

export function ledgerStreamQuery(state, offset = 0) {
  const search = (state.recentSearch || '').slice(0, 200);
  const flow = state.recentFlowFilter === 'internal_transfer' ? 'cash_transfer' : state.recentFlowFilter || 'cashflow';
  const params = new URLSearchParams({
    view: 'stream', period: state.recentPeriod, account: state.recentAccountFilter || '',
    flow, search,
    matchingAccounts: accountCodesForLabel(search).join(','),
    sort: state.recentAmountSort || '', offset,
  });
  if (state.recentAccountGroup) params.set('accountGroup', state.recentAccountGroup);
  if (flow === 'cashflow' || flow === 'cash_transfer') params.set('pairCards', 'true');
  return params.toString();
}

export function readLedgerStream(state, { offset, signal }) {
  return jget(`/api/ledger/workbench?${ledgerStreamQuery(state, offset)}`, { cache: false, signal });
}

async function updateLedger(path, body, options) {
  const invalidate = pageDataInvalidator();
  invalidate();
  try { return await jpost(path, body, options); }
  finally { invalidate(); }
}

export function updateLedgerRow(row, fields) {
  return updateLedger(`/api/ledger/rows/${encodeURIComponent(row.id)}/update`, fields);
}

export function readLedgerTransfers({ month, manualPending, signal }) {
  const params = new URLSearchParams({ view: 'transfers' });
  if (month) params.set('month', month);
  if (manualPending) params.set('manualPending', 'true');
  return jget(`/api/ledger/workbench?${params}`, { cache: false, signal });
}

export function readManualTransferOptions(anchor, offset) {
  const params = new URLSearchParams({ offset });
  return jget(`/api/bank-reconcile/transfers/manual-pending/${encodeURIComponent(anchor)}/options?${params}`, {
    cache: false,
  });
}

export function readTransferCandidates() {
  return jpost('/api/bank-reconcile/transfer-matches/run', {});
}

export function confirmTransferPair(pair) {
  return updateLedger('/api/bank-reconcile/transfers/confirm', {
    from_kind: pair.source_kind || 'cashflow', from_id: pair.source_row_id,
    to_kind: pair.target_kind || 'cashflow', to_id: pair.target_row_id,
    note: '在收支内部划转页人工核对确认', confirmed: true,
    ...(pair.manual_anchor_id ? {manual_anchor_id: pair.manual_anchor_id} : {}),
  }, {includeErrorDetail: true});
}
