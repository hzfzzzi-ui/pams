import { jget, jpost } from '../../api.js?v=20261031-gray-v1';

export function readLedgerWorkbench() {
  const params = new URLSearchParams({ baseCurrency: 'CNY', recentDays: '30' });
  return Promise.all([
    jget(`/api/ledger/workbench?${params.toString()}`, { cache: false }),
    jget('/api/accounts/overview').catch(() => ({ summary: {}, accounts: [] })),
  ]).then(([data, accounts]) => ({ data, accounts }));
}

export function updateLedgerRow(row, fields) {
  return jpost(`/api/ledger/rows/${encodeURIComponent(row.id)}/update`, fields);
}
