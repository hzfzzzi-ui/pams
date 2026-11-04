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
