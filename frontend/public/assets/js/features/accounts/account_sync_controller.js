import { isReadCancelled } from '../../shared/read_scope.js';
import { renderAccountSync } from './account_sync_view.js';

export function createAccountSyncLoader(root, { reads, isActive, compact = false }) {
  const panel = root.querySelector('#accountSyncPanel');
  const status = root.querySelector('#accountSyncStatus');
  const retry = root.querySelector('#retryAccountSync');
  return async function loadAccountSync() {
    if (!panel || !isActive()) return;
    retry.hidden = true;
    status.textContent = '正在读取账户状态…';
    try {
      const data = await reads.get('account-sync', '/api/sync/accounts', { cache: false });
      if (!isActive()) return;
      renderAccountSync(panel, data.groups, (code, offset) => reads.get('bank-source-anomalies:' + code,
        '/api/sync/accounts/' + encodeURIComponent(code) + '/source-anomalies?offset=' + offset, { cache: false }),
      { compact });
      status.textContent = '';
    } catch (error) {
      if (isActive() && !isReadCancelled(error)) {
        status.textContent = '暂时无法读取账户状态。';
        retry.hidden = false;
      }
    }
  };
}
