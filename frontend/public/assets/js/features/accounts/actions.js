// POST payloads and sequential follow-up reads retain the existing account flow.
// Disposal never cancels a submitted write; it suppresses later UI work/reads.
export function createActions({post, isActive, view, acceptOverview, loadUnmatchedRows, loadImports, loadAccountOverview}) {
  async function reviewAccount(accountCode, action) {
    if (!isActive()) return;
    view.clearError();
    const actionLabel = action === 'approve' ? '确认核准' : '暂缓缺数据';
    view.setMsg(`正在提交${actionLabel}...`);
    const note = action === 'approve' ? '账户核准中心确认当前余额' : '账户核准中心暂缓当前缺数据项';
    const data = await post(`/api/accounts/${encodeURIComponent(accountCode)}/review`, {action, note});
    if (!isActive()) return;
    acceptOverview(data);
    view.setMsg(`${actionLabel}完成：${accountCode}`);
  }

  async function runTransferMatching() {
    if (!isActive()) return;
    view.clearError();
    view.setMsg('正在匹配账户间流水...');
    const resp = await post('/api/bank-reconcile/transfer-matches/run', {});
    if (!isActive()) return;
    view.renderTransferMatches(resp);
    await loadUnmatchedRows();
    if (!isActive()) return;
    await loadImports();
    if (!isActive()) return;
    await loadAccountOverview();
    if (!isActive()) return;
    const count = (resp.matches || []).filter(row => row.status === 'review_required' && row.match_key?.startsWith('candidate:')).length;
    view.setMsg(`找到 ${count} 对流水候选，确认配对后才会扣减两边的收支。`);
  }

  async function confirmCashPair(fromId, toId) {
    await post('/api/bank-reconcile/transfers/confirm', {
      from_kind: 'cashflow', from_id: fromId, to_kind: 'cashflow', to_id: toId,
      note: '在收支配对页面人工核对确认', confirmed: true,
    });
    if (isActive()) await runTransferMatching();
  }

  return {reviewAccount, runTransferMatching, confirmCashPair};
}
