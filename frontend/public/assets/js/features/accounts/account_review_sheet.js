const explanations = {
  duplicates: count => ['疑似重复 · ' + count + ' 笔', '这些记录在同一账户内日期、币种和金额相同，可能是不同交易。系统未删除或合并任何记录。'],
  unposted_manual: count => ['手工流水未入账 · ' + count + ' 笔', '这些手工资料已经确认，账本里还没有对应流水。'],
  cashflow: () => ['流水来源待核对', '下列流水未通过来源核验。'],
  investment: () => ['投资交易来源待核对', '下列投资交易未通过来源核验。'],
  balance: () => ['余额来源待核对', '最新余额快照不唯一，或现有快照的来源证据不足。'],
  no_sync: () => ['暂无成功同步记录', '未找到已确认的同步或导入记录。'],
};

export function showAccountReview(panel, account, loadRecords, {balanceRequested = false} = {}) {
  const doc = panel.ownerDocument;
  const element = (tag, text) => {
    const node = doc.createElement(tag);
    node.textContent = text;
    return node;
  };
  const content = element('div');
  content.className = 'accountReviewDetails';
  panel.replaceChildren(content);
  const reasons = (account.summary?.review_reasons || []).filter(reason => Object.hasOwn(explanations, reason.kind));
  if (balanceRequested) {
    const data = account.summary || {};
    const flowEstimate = data.balance_basis === 'credit_card_flow_net';
    if (flowEstimate && data.balance_amount != null) {
      const amount = new Intl.NumberFormat('zh-CN', {style: 'currency', currency: data.balance_currency || account.currency}).format(data.balance_amount);
      content.append(element('p', `按已确认流水计算的欠款：${amount}${data.balance_as_of ? `（截至 ${data.balance_as_of}）` : ''}`));
    }
    content.append(element('p', flowEstimate
      ? '尚未核准账单余额。请用银行账单的余额和账单日期核准。'
      : '组合数据缺少此账户的已核准余额。请核对余额日期和来源。'));
  } else if (!reasons.length) {
    content.append(element('p', '暂未返回具体原因，请刷新后再查看。'));
  }
  for (const reason of reasons) {
    const {kind, count} = reason;
    const [title, description] = explanations[kind](count);
    const section = element('section');
    section.append(element('h4', title), element('p', description));
    content.append(section);
    if (kind === 'no_sync') continue;
    const records = element('ul');
    const status = element('p');
    status.setAttribute('role', 'status');
    const next = element('button', '加载记录');
    next.type = 'button';
    section.append(records, status, next);
    let offset = 0;
    async function load() {
      next.disabled = true;
      status.textContent = '正在读取记录…';
      try {
        const {rows} = await loadRecords(kind, offset);
        if (!content.isConnected) return;
        if (!Array.isArray(rows)) throw new Error('Invalid review rows');
        for (const row of rows) {
          const amount = row.amount == null ? null : [row.amount, row.currency].filter(Boolean).join(' ');
          const record = element('li');
          const position = row.source_position?.row;
          record.append(element('div', [row.day, row.summary, amount,
            row.record_id == null ? null : '记录 #' + row.record_id].filter(Boolean).join(' · ')));
          if (row.source_id) {
            const link = element('a', [row.filename || '查看原件', position == null ? null : '第 ' + position + ' 行'].filter(Boolean).join(' · '));
            link.href = '/api/account-import/source-records/' + encodeURIComponent(row.source_id) + '/original';
            record.append(link);
          }
          records.append(record);
        }
        offset += rows.length;
        status.textContent = offset ? '' : '当前没有可展示的关联记录。';
        next.hidden = rows.length < 100 || (count != null && offset >= count);
        next.textContent = '加载更多';
      } catch {
        if (content.isConnected) {
          status.textContent = '记录读取失败。';
          next.textContent = '重试';
        }
      } finally {
        next.disabled = false;
      }
    }
    next.addEventListener('click', load);
    void load();
  }
}
