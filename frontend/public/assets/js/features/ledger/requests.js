import { readFallback } from '../../shared/read_scope.js';
import { jget, jpost } from '../../api.js';

export function readLedgerWorkbench({signal} = {}) {
  const params = new URLSearchParams({ baseCurrency: 'CNY', recentDays: '30' });
  return Promise.all([
    jget(`/api/ledger/workbench?${params.toString()}`, { cache: false, signal }),
    jget('/api/accounts/overview', {signal}).catch(error => readFallback(error, { summary: {}, accounts: [] })),
  ]).then(([data, accounts]) => ({ data, accounts }));
}

export function updateLedgerRow(row, fields) {
  return jpost(`/api/ledger/rows/${encodeURIComponent(row.id)}/update`, fields);
}

export function readLedgerTransfers({ month, signal }) {
  const params = new URLSearchParams({ includeTransferPairs: 'true' });
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
