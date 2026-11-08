import { isReadCancelled, readFallback } from '../../shared/read_scope.js';
import { renderAccountSync } from './account_sync_view.js';

const portfolioAccounts = new Set(['tonghuashun', 'ibkr']);

function withPortfolioAmounts(groups, holdings) {
  const entries = holdings?.base_currency === 'CNY' && Array.isArray(holdings.by_account) ? holdings.by_account : [];
  const values = new Map(entries.map(row => [row.account_code, row]));
  return groups.map(group => ({...group, rows: group.rows.map(row => {
    if (!row.active || !portfolioAccounts.has(row.account_code)) return row;
    const value = values.get(row.account_code);
    const fullAmount = value?.total_value_base;
    const amount = fullAmount ?? value?.known_value_base ?? null;
    return {...row, summary: {...row.summary, balance_amount: amount,
      balance_currency: holdings?.base_currency, balance_as_of: holdings?.as_of,
      balance_label: amount == null ? null : fullAmount == null ? '已覆盖市值' : '组合价值'}};
  })}));
}

export function createAccountSyncLoader(root, { reads, isActive, compact = false, openReview }) {
  const panel = root.querySelector('#accountSyncPanel');
  const status = root.querySelector('#accountSyncStatus');
  const retry = root.querySelector('#retryAccountSync');
  return async function loadAccountSync() {
    if (!panel || !isActive()) return;
    retry.hidden = true;
    status.textContent = '正在读取账户状态…';
    try {
      const [data, holdings] = await Promise.all([
        reads.get('account-sync', '/api/sync/accounts', {cache: false}),
        compact ? reads.get('account-holdings', '/api/holdings/by-account?base_currency=CNY', {cache: false})
          .catch(error => readFallback(error, null)) : null,
      ]);
      if (!isActive()) return;
      const groups = compact ? withPortfolioAmounts(data.groups, holdings) : data.groups;
      renderAccountSync(panel, groups, (code, offset) => reads.get('bank-source-anomalies:' + code,
        '/api/sync/accounts/' + encodeURIComponent(code) + '/source-anomalies?offset=' + offset, { cache: false }),
      { compact, openReview });
      status.textContent = '';
    } catch (error) {
      if (isActive() && !isReadCancelled(error)) {
        status.textContent = '暂时无法读取账户状态。';
        retry.hidden = false;
      }
    }
  };
}
