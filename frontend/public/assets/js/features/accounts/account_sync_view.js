const localTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

function describe(label, data, balanceCurrency = null) {
  const isBalance = balanceCurrency !== null;
  const status = { recorded: isBalance ? '已确认' : '已入库', review: '来源待核对',
    uninitialized: isBalance ? '尚未确认' : '尚未入库' };
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

const compactStatuses = { normal: '正常', review: '待核查', stale: '超月未同步', inactive: '已停用' };

export function renderAccountSync(panel, groups, loadAnomalies, { compact = false } = {}) {
  if (!Array.isArray(groups)) throw new Error('账户同步状态格式异常');
  panel.replaceChildren();
  const doc = panel.ownerDocument;
  function element(tag, text) {
    const node = doc.createElement(tag);
    node.textContent = text;
    return node;
  }
  function compactEntry(item, row) {
    const data = row.summary || {};
    const heading = element('div');
    heading.className = 'accountSyncCompactHeading';
    const name = element('div');
    name.className = 'accountSyncCompactName';
    const status = Object.hasOwn(compactStatuses, data.status) ? data.status : 'review';
    const badge = element('span', compactStatuses[status]);
    badge.className = 'accountSyncBadge';
    badge.dataset.status = status;
    name.append(element('h4', row.label), badge);
    const amount = data.balance_amount;
    const balance = element('span', amount == null ? '—' :
      new Intl.NumberFormat('zh-CN', { style: 'currency', currency: row.currency }).format(amount));
    balance.className = 'accountSyncAmount';
    balance.dataset.sign = Number(amount) > 0 ? 'positive' : Number(amount) < 0 ? 'negative' : 'neutral';
    balance.setAttribute('aria-label', '余额 ' + balance.textContent);
    heading.append(name, balance);
    const metadata = element('dl');
    const syncedAt = data.last_synced_at ? new Date(data.last_synced_at) : null;
    const syncedLabel = syncedAt && Number.isFinite(syncedAt.valueOf())
      ? localTime.format(syncedAt).replaceAll('/', '-') : '—';
    metadata.append(element('dt', '最近交易'), element('dd', data.latest_trade_on || '—'),
      element('dt', '上次同步'), element('dd', syncedLabel));
    item.classList.add('accountSyncCompact');
    item.append(heading, metadata);
  }
  for (const group of groups) {
    const section = element('details');
    section.className = 'accountSyncGroup';
    section.open = compact;
    const summary = element('summary', group.label);
    const arrow = element('span', '›');
    arrow.setAttribute('aria-hidden', 'true');
    summary.append(arrow);
    section.append(summary);
    for (const row of group.rows) {
      const item = element('article');
      item.className = 'accountSyncEntry';
      if (compact) {
        compactEntry(item, row);
        section.append(item);
        continue;
      }
      item.append(element('h4', `${row.label} · ${row.currency}`));
      if (!row.active) {
        item.append(element('p', '已停用'));
      } else {
        if (row.source_data) {
          const source = row.source_data;
          item.append(element('p', source.documents
            ? '来源已确认 · ' + source.documents + ' 份 · 流水明细 ' + source.count + ' 笔'
            : '尚无已确认的来源资料'));
          if (source.duplicate_count > 0 && loadAnomalies) {
            const details = element('details');
            const rows = element('div');
            const next = element('button', '加载明细');
            next.type = 'button';
            let offset = 0;
            async function load() {
              if (next.disabled) return;
              next.disabled = true;
              next.textContent = '读取中…';
              try {
                const result = await loadAnomalies(row.account_code, offset);
                if (!details.isConnected) return;
                for (const entry of result.rows) {
                  const link = element('a', [entry.day, entry.summary, entry.amount + ' ' + entry.currency,
                    entry.filename, entry.source_position?.row == null ? null : '行 ' + entry.source_position.row]
                    .filter(Boolean).join(' · '));
                  link.href = '/api/account-import/source-records/' + encodeURIComponent(entry.source_id) + '/original';
                  const line = element('p');
                  line.append(link);
                  rows.append(line);
                }
                offset += result.rows.length;
                next.hidden = offset >= source.duplicate_count || result.rows.length < 100;
                next.textContent = '加载更多';
              } catch {
                next.textContent = '读取失败，点击重试';
              } finally { next.disabled = false; }
            }
            next.addEventListener('click', load);
            details.addEventListener('toggle', () => {
              if (details.open && offset === 0 && !next.hidden) load();
            });
            details.append(element('summary', '疑似重复 · ' + source.duplicate_count + ' 笔'), rows, next);
            item.append(details);
          }
        } else {
          item.append(element('p', '来源确认 · 暂无汇总'));
        }
        item.append(element('p', describe('流水', row.cashflow)));
        if (row.show_investment) item.append(element('p', describe('投资交易', row.investment)));
        item.append(element('p', describe('余额', row.balance, row.currency)));
        const review = element('button', row.balance.status === 'recorded' ? '查看余额' : '核对余额');
        review.type = 'button';
        review.className = 'btn';
        review.dataset.syncBalanceReview = row.account_code;
        item.append(review);
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
