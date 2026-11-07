const localTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

function describe(label, data, balanceCurrency = null) {
  const isBalance = balanceCurrency !== null;
  const status = { recorded: isBalance ? '已初始化' : '已入库', review: '来源待核对', uninitialized: '待初始化' };
  const parts = [`${label} ${status[data.status]}`];
  if (isBalance && data.amount != null) {
    parts.push(new Intl.NumberFormat('zh-CN', { style: 'currency', currency: balanceCurrency }).format(data.amount));
  }
  if (!isBalance && data.count > 0) parts.push(`${data.count} 笔`);
  if (data.as_of) parts.push(`${isBalance ? '余额日' : '最近一笔'} ${data.as_of}`);
  if (data.synced_at) parts.push(`上次同步 ${localTime.format(new Date(data.synced_at))}`);
  if (data.status === 'recorded' && data.has_unverified) parts.push('另有记录待核对');
  return parts.join(' · ');
}

export function renderAccountSync(panel, groups) {
  if (!Array.isArray(groups)) throw new Error('账户同步状态格式异常');
  panel.replaceChildren();
  const doc = panel.ownerDocument;
  function element(tag, text) {
    const node = doc.createElement(tag);
    node.textContent = text;
    return node;
  }
  for (const group of groups) {
    const section = doc.createElement('details');
    section.append(element('summary', group.label));
    for (const row of group.rows) {
      const item = doc.createElement('section');
      item.append(element('strong', `${row.label} · ${row.currency}`));
      if (!row.active) {
        item.append(element('p', '已停用'));
      } else {
        item.append(element('p', describe('流水', row.cashflow)));
        if (row.show_investment) item.append(element('p', describe('投资交易', row.investment)));
        item.append(element('p', describe('余额', row.balance, row.currency)));
      }
      if (row.balance.historical_as_of) {
        item.append(element('p', `历史参考 · ${row.balance.historical_as_of}`));
      }
      if (row.can_import) {
        const link = element('a', '导入资料');
        link.href = `account_import.html?account=${encodeURIComponent(row.account_code)}`;
        item.append(link);
      }
      section.append(item);
    }
    panel.append(section);
  }
  if (!groups.length) panel.textContent = '暂无可显示的账户。';
}
