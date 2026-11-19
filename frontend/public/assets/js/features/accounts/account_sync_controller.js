import { isReadCancelled, readFallback } from '../../shared/read_scope.js';
import { renderAccountSync } from './account_sync_view.js';

function withPortfolioBalances(groups, balances) {
  return groups.map(group => ({ ...group, rows: group.rows.map(row => {
    const balance = row.active && balances?.[row.account_code];
    const currentDay = row.summary?.balance_amount != null && row.summary?.balance_as_of;
    return balance && (!currentDay || balance.balance_as_of >= currentDay) ? { ...row, summary: { ...row.summary, ...balance } } : row;
  }) }));
}

export function createAccountSyncLoader(root, { reads, isActive, compact = false, openReview, openAccount, targetAccount, targetInstitution, onRendered }) {
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
        compact ? reads.get('account-holdings', '/api/investment/account-center-balances', {cache: false})
          .catch(error => readFallback(error, null)) : null,
      ]);
      if (!isActive()) return;
      const groups = compact ? withPortfolioBalances(data.groups, holdings) : data.groups;
      renderAccountSync(panel, groups, (code, offset) => reads.get('bank-source-anomalies:' + code,
        '/api/sync/accounts/' + encodeURIComponent(code) + '/source-anomalies?offset=' + offset, { cache: false }),
      { compact, openReview, openAccount, targetAccount, targetInstitution });
      status.textContent = '';
      onRendered?.(panel, groups);
    } catch (error) {
      if (isActive() && !isReadCancelled(error)) {
        status.textContent = '暂时无法读取账户状态。';
        retry.hidden = false;
      }
    }
  };
}
