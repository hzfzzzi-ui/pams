import { subaccountImportHref } from '../../app/routes.js';
import { accountInstitution } from '../../shared/account_institutions.js';

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

const compactStatuses = { normal: '正常', review: '待核查', stale: '待刷新', inactive: '已停用' };

function element(doc, tag, text = '') {
  const node = doc.createElement(tag);
  node.textContent = text;
  return node;
}

export function accountSyncMetadata(doc, data = {}, { compact = false } = {}) {
  const metadata = element(doc, 'dl');
  metadata.className = 'accountSyncMetadata';
  const valuationAt = data.valuation_updated_at ? new Date(data.valuation_updated_at) : null;
  const hasValuationTime = Number.isFinite(valuationAt?.valueOf());
  const syncAt = data.last_synced_at ? new Date(data.last_synced_at) : null;
  const updatedAt = hasValuationTime ? valuationAt : syncAt;
  const updatedLabel = Number.isFinite(updatedAt?.valueOf())
    ? localTime.format(updatedAt).replaceAll('/', '-') : data.last_synced_on || '—';
  if (compact) {
    const syncLabel = Number.isFinite(syncAt?.valueOf())
      ? localTime.format(syncAt).replaceAll('/', '-') : data.last_synced_on || '—';
    metadata.append(element(doc, 'dt', '最近交易'), element(doc, 'dd', data.latest_trade_on || '—'),
      element(doc, 'dt', '上次同步'), element(doc, 'dd', syncLabel));
    return metadata;
  }
  if (Object.hasOwn(data, 'latest_source_on')) {
    metadata.append(element(doc, 'dt', '最近资料'), element(doc, 'dd', data.latest_source_on || '—'),
      element(doc, 'dt', '最近入账'), element(doc, 'dd', data.latest_cashflow_on || '—'));
  } else {
    metadata.append(element(doc, 'dt', '最近交易'), element(doc, 'dd', data.latest_trade_on || '—'));
  }
  const money = new Intl.NumberFormat('zh-CN', {style: 'currency', currency: data.balance_currency || 'CNY'});
  if (data.position_value != null) {
    metadata.append(element(doc, 'dt', '持仓 · ' + data.position_as_of),
      element(doc, 'dd', money.format(data.position_value)));
  }
  if (data.position_debt != null) {
    metadata.append(element(doc, 'dt', '负债 · ' + data.position_debt_as_of),
      element(doc, 'dd', money.format(data.position_debt)));
  }
  metadata.append(element(doc, 'dt', hasValuationTime ? '估值更新' : '上次同步'), element(doc, 'dd', updatedLabel));
  return metadata;
}

function shortAccountLabel(label, groupLabel, shortLabel) {
  for (const prefix of [groupLabel, shortLabel]) {
    if (prefix && label.startsWith(prefix)) {
      const short = label.slice(prefix.length).trim();
      if (short) return short.replace(/^(\d{4})(?=[^\d\s])/, '$1 ');
    }
  }
  return label;
}

function renderInstitutionPicker(panel, groups, renderEntry, targetAccount, preferredGroup) {
  const doc = panel.ownerDocument;
  const picker = element(doc, 'div');
  picker.className = 'accountInstitutionPicker';
  picker.setAttribute('role', 'group');
  picker.setAttribute('aria-label', '选择机构');
  const entries = groups.map((group, index) => {
    const {label: shortLabel, logoUrl} = accountInstitution(group.code, group.label);
    const button = element(doc, 'button');
    button.type = 'button';
    button.className = 'accountInstitutionButton';
    button.dataset.institution = group.code || group.label;
    const logo = element(doc, 'span', shortLabel.slice(0, 1));
    logo.className = 'accountInstitutionLogo';
    logo.setAttribute('aria-hidden', 'true');
    if (logoUrl) {
      const image = element(doc, 'img');
      image.alt = '';
      image.src = logoUrl;
      image.addEventListener('error', () => image.remove(), {once: true});
      logo.append(image);
    }
    const label = element(doc, 'span', shortLabel);
    label.className = 'accountInstitutionLabel';
    button.append(logo, label);
    const statuses = group.rows.filter(row => row.active !== false).map(row => row.summary?.status);
    const issue = statuses.includes('review') ? 'review' : statuses.includes('stale') ? 'stale' : null;
    if (issue) {
      const flag = element(doc, 'span', '!');
      flag.className = 'accountInstitutionFlag';
      flag.dataset.status = issue;
      flag.setAttribute('aria-hidden', 'true');
      button.append(flag);
    }
    button.setAttribute('aria-label', [group.label, compactStatuses[issue]].filter(Boolean).join('，'));
    const section = element(doc, 'section');
    section.className = 'accountInstitutionPanel';
    section.id = 'accountInstitutionPanel-' + index;
    button.setAttribute('aria-controls', section.id);
    const heading = element(doc, 'header');
    const title = element(doc, 'h3', group.label);
    title.id = section.id + '-title';
    section.setAttribute('aria-labelledby', title.id);
    heading.append(title, element(doc, 'small', group.rows.length + ' 个子账户'));
    section.append(heading);
    for (const row of group.rows) {
      const item = element(doc, 'article');
      item.className = 'accountSyncEntry';
      if (row.account_code) item.dataset.accountCode = row.account_code;
      renderEntry(item, row, shortAccountLabel(row.label, group.label, shortLabel));
      section.append(item);
    }
    picker.append(button);
    return { group, button, section };
  });
  function select(current) {
    for (const entry of entries) {
      entry.button.setAttribute('aria-pressed', String(entry === current));
      entry.section.hidden = entry !== current;
    }
    if (current && picker.isConnected) {
      const pickerRect = picker.getBoundingClientRect();
      const buttonRect = current.button.getBoundingClientRect();
      if (buttonRect.left < pickerRect.left) picker.scrollLeft -= pickerRect.left - buttonRect.left;
      else if (buttonRect.right > pickerRect.right) picker.scrollLeft += buttonRect.right - pickerRect.right;
    }
  }
  for (const entry of entries) entry.button.addEventListener('click', () => select(entry));
  panel.append(picker, ...entries.map(entry => entry.section));
  select(entries.find(entry => targetAccount && entry.group.rows.some(row => row.account_code === targetAccount)) ||
    entries.find(entry => entry.button.dataset.institution === preferredGroup) || entries[0]);
}

function freshnessInfo(data = {}) {
  const candidates = [
    data.valuation_updated_at,
    data.last_synced_at,
    data.balance_as_of,
    data.latest_cashflow_on,
    data.latest_trade_on,
    data.latest_source_on,
    data.last_synced_on,
  ].filter(Boolean);
  let latestMs = null;
  for (const raw of candidates) {
    const text = String(raw);
    const parsed = /^\d{4}-\d{2}-\d{2}$/.test(text) ? Date.parse(`${text}T12:00:00+08:00`) : Date.parse(text);
    if (Number.isFinite(parsed) && (latestMs === null || parsed > latestMs)) latestMs = parsed;
  }
  if (latestMs === null) return { label: '未同步', tone: 'stale' };
  const days = Math.max(0, Math.floor((Date.now() - latestMs) / 86400000));
  if (days === 0) return { label: '今日', tone: 'fresh' };
  if (days <= 7) return { label: `${days}天前`, tone: 'fresh' };
  if (days <= 30) return { label: `${days}天前`, tone: 'warn' };
  return { label: `${days}天前`, tone: 'stale' };
}

function recentSixMonths() {
  const now = new Date();
  const months = [];
  for (let i = 5; i >= 0; i -= 1) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    months.push({ key: `${y}-${m}`, label: `${Number(m)}月` });
  }
  return months;
}

function buildCoverageMatrix(doc, groups) {
  const activeRows = groups.flatMap(g => g.rows).filter(r => r.active !== false);
  if (!activeRows.length) return null;
  const months = recentSixMonths();
  const details = element(doc, 'details');
  details.className = 'accountCoverageMatrix';
  const summary = element(doc, 'summary', '近 6 个月数据覆盖');
  const gapCount = activeRows.reduce((acc, row) => {
    const set = new Set(row.summary?.covered_months || []);
    return acc + months.filter(m => !set.has(m.key)).length;
  }, 0);
  const meta = element(doc, 'small', gapCount > 0 ? `${gapCount} 处月份缺口` : '全部已覆盖');
  summary.append(meta);
  details.append(summary);
  const table = element(doc, 'div');
  table.className = 'coverageGrid';
  const header = element(doc, 'div');
  header.className = 'coverageRow coverageHead';
  header.append(element(doc, 'span', '账户'));
  for (const m of months) header.append(element(doc, 'span', m.label));
  table.append(header);
  for (const row of activeRows) {
    const covered = new Set(row.summary?.covered_months || []);
    const line = element(doc, row.can_import ? 'a' : 'div');
    line.className = 'coverageRow';
    if (row.can_import) {
      line.href = subaccountImportHref(row.account_code);
      line.title = `前往导入 · ${row.label}`;
    }
    const nameEl = element(doc, 'span', row.label);
    nameEl.className = 'coverageAccount';
    line.append(nameEl);
    for (const m of months) {
      const dot = element(doc, 'span', covered.has(m.key) ? '●' : '○');
      dot.className = covered.has(m.key) ? 'coverageDot is-hit' : 'coverageDot is-gap';
      dot.setAttribute('aria-label', `${row.label} ${m.key} ${covered.has(m.key) ? '有数据' : '无数据'}`);
      line.append(dot);
    }
    table.append(line);
  }
  details.append(table);
  return details;
}

export function renderAccountSync(panel, groups, loadAnomalies, { compact = false, openReview, openAccount, targetAccount, targetInstitution } = {}) {
  if (!Array.isArray(groups)) throw new Error('账户同步状态格式异常');
  const previousGroup = compact
    ? panel.querySelector('.accountInstitutionButton[aria-pressed="true"]')?.dataset.institution : null;
  panel.replaceChildren();
  const doc = panel.ownerDocument;
  const make = (tag, text) => element(doc, tag, text);
  function compactEntry(item, row, label) {
    const data = row.summary || {};
    const heading = make('div');
    heading.className = 'accountSyncCompactHeading';
    const name = make('div');
    name.className = 'accountSyncCompactName';
    const status = Object.hasOwn(compactStatuses, data.status) ? data.status : 'review';
    const actionable = status === 'review' && openReview;
    const badge = make(actionable ? 'button' : 'span', compactStatuses[status]);
    if (actionable) {
      badge.type = 'button';
      badge.setAttribute('aria-label', row.label + '：待核查');
      badge.setAttribute('aria-haspopup', 'dialog');
      badge.addEventListener('click', () => openReview(row));
    }
    badge.className = 'accountSyncBadge';
    badge.dataset.status = status;
    const title = make('h4', label);
    if (openAccount) {
      const button = make('button', label);
      button.type = 'button';
      button.className = 'accountSyncNameButton';
      button.setAttribute('aria-label', row.label);
      button.setAttribute('aria-haspopup', 'dialog');
      button.addEventListener('click', () => openAccount(row));
      title.replaceChildren(button);
    }
    name.append(title, badge);
    if (status !== 'inactive') {
      const fresh = freshnessInfo(data);
      const freshBadge = make('span', fresh.label);
      freshBadge.className = `accountFreshnessBadge is-${fresh.tone}`;
      name.append(freshBadge);
    }
    const amount = data.balance_amount;
    const balance = make('span', amount == null ? '—' :
      new Intl.NumberFormat('zh-CN', { style: 'currency', currency: data.balance_currency || row.currency }).format(amount));
    balance.className = 'accountSyncAmount';
    balance.dataset.sign = Number(amount) > 0 ? 'positive' : Number(amount) < 0 ? 'negative' : 'neutral';
    const amountLabel = data.balance_label || '余额';
    balance.setAttribute('aria-label', amountLabel + ' ' + balance.textContent);
    if (amount != null) {
      const value = make('div');
      value.className = 'accountSyncAmountWithLabel';
      const label = make('small', [amountLabel, data.balance_as_of].filter(Boolean).join(' · '));
      value.append(balance, label);
      heading.append(name, value);
    } else {
      heading.append(name, balance);
    }
    item.classList.add('accountSyncCompact');
    item.append(heading);
    if (status !== 'inactive' && openAccount && row.can_import) {
      const foot = make('div');
      foot.className = 'accountSyncCompactFoot';
      const quickImport = make('a', `导入 ${label} ›`);
      quickImport.className = 'accountSyncCompactImport';
      quickImport.href = subaccountImportHref(row.account_code);
      quickImport.setAttribute('aria-label', '导入资料 · ' + row.label);
      foot.append(quickImport);
      item.append(foot);
    }
    if (status !== 'inactive' && !openAccount) {
      item.append(accountSyncMetadata(doc, data, { compact: true }));
      const action = make(row.can_import ? 'a' : 'button', '导入资料');
      action.className = 'accountSyncImport';
      if (row.can_import) {
        action.href = subaccountImportHref(row.account_code);
        action.setAttribute('aria-label', '导入资料 · ' + row.label);
      } else {
        action.type = 'button';
        action.disabled = true;
        action.title = '该子账户暂未开放导入';
      }
      item.append(action);
    }
  }
  if (compact && groups.length) {
    renderInstitutionPicker(panel, groups, compactEntry, targetAccount, previousGroup || targetInstitution);
    const matrix = buildCoverageMatrix(doc, groups);
    if (matrix) panel.append(matrix);
    return;
  }
  for (const group of groups) {
    const section = make('details');
    section.className = 'accountSyncGroup';
    const summary = make('summary', group.label);
    const arrow = make('span', '›');
    arrow.setAttribute('aria-hidden', 'true');
    summary.append(arrow);
    section.append(summary);
    for (const row of group.rows) {
      const item = make('article');
      item.className = 'accountSyncEntry';
      if (row.account_code) item.dataset.accountCode = row.account_code;
      item.append(make('h4', `${row.label} · ${row.currency}`));
      if (!row.active) {
        item.append(make('p', '已停用'));
      } else {
        if (row.source_data) {
          const source = row.source_data;
          item.append(make('p', source.documents
            ? '来源已确认 · ' + source.documents + ' 份 · 流水明细 ' + source.count + ' 笔'
            : '尚无已确认的来源资料'));
          if (source.duplicate_count > 0 && loadAnomalies) {
            const details = make('details');
            const rows = make('div');
            const next = make('button', '加载明细');
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
                  const link = make('a', [entry.day, entry.summary, entry.amount + ' ' + entry.currency,
                    entry.filename, entry.source_position?.row == null ? null : '行 ' + entry.source_position.row]
                    .filter(Boolean).join(' · '));
                  link.href = '/api/account-import/source-records/' + encodeURIComponent(entry.source_id) + '/original';
                  const line = make('p');
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
            details.append(make('summary', '疑似重复 · ' + source.duplicate_count + ' 笔'), rows, next);
            item.append(details);
          }
        } else {
          item.append(make('p', '来源确认 · 暂无汇总'));
        }
        item.append(make('p', describe('流水', row.cashflow)));
        if (row.show_investment) item.append(make('p', describe('投资交易', row.investment)));
        item.append(make('p', describe('余额', row.balance, row.currency)));
        const review = make('button', row.balance.status === 'recorded' ? '查看余额' : '核对余额');
        review.type = 'button';
        review.className = 'btn';
        review.dataset.syncBalanceReview = row.account_code;
        item.append(review);
      }
      if (row.balance.historical_as_of) {
        item.append(make('p', `历史参考 · ${row.balance.historical_as_of}`));
      }
      if (row.can_import) {
        const link = make('a', '导入资料');
        link.href = subaccountImportHref(row.account_code);
        item.append(link);
      }
      section.append(item);
    }
    panel.append(section);
  }
  if (!groups.length) panel.textContent = '暂无可显示的账户。';
}
