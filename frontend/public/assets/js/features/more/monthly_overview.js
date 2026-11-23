import {esc, fmtCny} from '../investment/holdings/summary_legacy_model.js';

const metrics = [
  {key: 'invest', label: '持仓盈亏', amount: 'pnl', heading: '投资分类'},
  {key: 'receipts', label: '账面收入', amount: 'inc', heading: '收入来源'},
  {key: 'payments', label: '账面支出', amount: 'exp', heading: '支出去向'},
];
const exact = n => Number.isFinite(n) ? fmtCny(n) : '未列示';
const compact = n => !Number.isFinite(n) ? '—' : Math.abs(n) >= 10000
  ? (n / 10000).toFixed(2) + '万' : n.toLocaleString('zh-CN', {maximumFractionDigits: 2});
const signed = n => (n > 0 ? '+' : '') + compact(n);

function focusText({delta, gap, pnl, cash}) {
  if ([delta, gap, pnl, cash].some(n => !Number.isFinite(n))) return '部分月末数据待核';
  if (Math.sign(gap) === Math.sign(delta) && Math.abs(gap) > Math.max(Math.abs(pnl), Math.abs(cash))) {
    return '本月最大差额仍待核对';
  }
  if (cash > 0 && pnl < -cash) return '持仓亏损超过收支结余';
  if (cash > 0 && pnl > 0) return '收支有结余，持仓也盈利';
  return '账面结余 ' + signed(cash) + ' · 可比持仓 ' + signed(pnl);
}

function renderTrend(svg, months, index) {
  const width = svg.clientWidth;
  if (!width || !months.length) return;
  const values = [months[0].open, ...months.map(row => row.nav)];
  const known = values.filter(Number.isFinite);
  const low = Math.min(...known), high = Math.max(...known);
  const x = i => 12 + i * (width - 24) / (values.length - 1);
  const y = n => high === low ? 24 : 39 - (n - low) * 32 / (high - low);
  const selected = index + 1;
  svg.setAttribute('viewBox', '0 0 ' + width + ' 62');
  const marks = [];
  values.forEach((n, i) => {
    if (Number.isFinite(n)) {
      if (i && Number.isFinite(values[i - 1])) {
        marks.push('<line class="' + (i === selected ? 'active' : '') + '" x1="' + x(i - 1) +
          '" y1="' + y(values[i - 1]) + '" x2="' + x(i) + '" y2="' + y(n) + '"/>');
      }
      if (i === selected || i === index) marks.push('<circle cx="' + x(i) + '" cy="' + y(n) + '" r="3"/>');
    }
    const nearby = Math.min(Math.abs(i - index), Math.abs(i - selected));
    if (!nearby || (nearby > 1 && (i === 0 || i === values.length - 1 || i === Math.floor(values.length / 2)))) {
      marks.push('<text x="' + x(i) + '" y="58" text-anchor="middle">' +
        (i ? months[i - 1].m + '月' : '年初') + '</text>');
    }
  });
  months.forEach((_, i) => marks.push('<rect data-index="' + i + '" x="' + (x(i + 1) - (x(1) - x(0)) / 2) +
    '" y="0" width="' + (x(1) - x(0)) + '" height="62" fill="transparent"/>'));
  svg.innerHTML = marks.join('');
}

function categoryRows(previous, current, key, parent) {
  const groups = month => month?.[key] || {};
  const data = month => parent === null ? groups(month) : groups(month)[parent]?.parts || {};
  const before = data(previous), after = data(current);
  const amount = value => parent === null ? value?.total : value;
  return [...new Set([...Object.keys(before), ...Object.keys(after)])].map(name => ({
    name, before: amount(before[name]), after: amount(after[name]),
  })).sort((a, b) => Math.max(Math.abs(b.before || 0), Math.abs(b.after || 0)) -
    Math.max(Math.abs(a.before || 0), Math.abs(a.after || 0)));
}

function pairedBars(entries, limit) {
  const values = entries.flatMap(row => [row.before, row.after]).filter(Number.isFinite);
  const minimum = Math.min(0, ...values), maximum = Math.max(0, ...values);
  const position = n => 3 + 94 * (n - minimum) / (maximum - minimum || 1);
  const zero = position(0);
  function bar(n, series) {
    if (!Number.isFinite(n)) return '';
    const end = position(n);
    return '<i class="bar ' + series + (n === 0 ? ' measuredZero' : '') + '" style="left:' +
      Math.min(end, zero) + '%;width:' + Math.abs(end - zero) + '%"></i>';
  }
  return entries.slice(0, limit).map(({name, before, after}, i) =>
    '<button type="button" class="plotRow" data-entry="' + i + '" aria-pressed="false" aria-label="' +
    esc(name + '，上月 ' + exact(before) + '，本月 ' + exact(after)) + '"><span class="rowName">' +
    esc(name.split(' · ')[0]) + '</span><span class="axis" aria-hidden="true"><i class="zero" style="left:' +
    zero + '%"></i>' + bar(before, 'previous') + bar(after, 'current') +
    '</span><span class="rowValue">' + compact(after) + '</span></button>').join('');
}

export function createMonthlyOverview(root, {win, read, isActive}) {
  const q = selector => root.querySelector(selector);
  const select = q('select'), graph = q('.graph'), tip = q('.monthlyTip'), svg = q('.trend');
  const listeners = new AbortController();
  const on = (target, type, handler) => target.addEventListener(type, handler, {signal: listeners.signal});
  let months = [], index = 0, metric = metrics[0], parent = null, all = false, entries = [];
  let pinned = false, active = null, loading = false;

  function dismiss() {
    if (active?.hasAttribute('data-entry')) active.setAttribute('aria-pressed', 'false');
    active = null;
    pinned = false;
    tip.hidden = true;
  }
  function drawTrend() { renderTrend(svg, months, index); }
  function render() {
    dismiss();
    const month = months[index], previous = months[index - 1];
    select.selectedIndex = index;
    q('[data-period]').textContent = month.m + ' 月末个人净资产';
    q('[data-scope]').textContent = month.partial ? '已覆盖账户' : '';
    q('[data-nav]').textContent = exact(month.nav);
    q('[data-change]').textContent = '较上月 ' + signed(month.delta);
    q('.monthlyFocus').textContent = focusText(month);
    q('.metricTabs').innerHTML = metrics.map(({key, label, amount}) =>
      '<button type="button" data-key="' + key + '" aria-pressed="' + (key === metric.key) + '">' +
      label + '<b>' + compact(month[amount]) + '</b></button>').join('');
    q('[data-heading]').innerHTML = parent === null ? '<h3>' + metric.heading + '</h3>'
      : '<button type="button" data-back>‹ ' + esc(parent) + '</button>';
    q('.legend').innerHTML = (previous ? '<span><i></i>' + previous.m + ' 月</span>' : '') +
      '<span><i class="current"></i>' + month.m + ' 月</span>';
    entries = categoryRows(previous, month, metric.key, parent);
    graph.innerHTML = pairedBars(entries, all ? entries.length : 4) || '<small>暂无可比分类</small>';
    q('[data-more]').hidden = entries.length <= 4;
    q('[data-more]').textContent = all ? '收起' : '全部 ' + entries.length + (parent === null ? ' 类 ›' : ' 项 ›');
    q('.monthlyAudit summary').textContent = '待核差额　' + signed(month.gap);
    q('.monthlyAudit p').textContent = '净资产变化 ' + exact(month.delta) + '，账面结余 ' + exact(month.cash) +
      '，投资盈亏 ' + exact(month.pnl) +
      Object.entries(month.adjustments || {}).map(([name, amount]) => '，' + name + ' ' + exact(amount)).join('') + '。';
    q('[data-assets]').textContent = month.assets == null ? '待结账' : exact(month.assets);
    q('[data-debts]').textContent = month.debts == null ? '待结账' : exact(month.debts);
    drawTrend();
  }
  function show(button, lock = false) {
    dismiss();
    const choice = metrics.find(item => item.key === button.dataset.key);
    const row = choice ? {name: choice.label, before: months[index - 1]?.[choice.amount],
      after: months[index][choice.amount]} : entries[Number(button.dataset.entry)];
    if (!row) return;
    active = button;
    pinned = lock;
    if (!choice) button.setAttribute('aria-pressed', String(lock));
    const line = (label, n) => '<div><span>' + label + '</span><b>' + exact(n) + '</b></div>';
    tip.innerHTML = '<strong>' + esc(row.name) + '</strong>' +
      (index ? line(months[index - 1].m + ' 月', row.before) : '') + line(months[index].m + ' 月', row.after) +
      (Number.isFinite(row.before) && Number.isFinite(row.after) ? line('较上月', row.after - row.before) : '') +
      (!choice && parent === null ? '<button type="button" data-drill>查看' +
        (metric.key === 'invest' ? '标的' : '子类') + ' ›</button>' : '');
    tip.hidden = false;
    const anchor = button.getBoundingClientRect(), box = root.getBoundingClientRect();
    tip.style.left = Math.max(4, Math.min(root.clientWidth - tip.offsetWidth - 5, anchor.left - box.left)) + 'px';
    tip.style.top = Math.max(2, anchor.top - box.top - tip.offsetHeight + 2) + 'px';
  }
  function selectMonth(i) {
    index = i;
    parent = null;
    all = false;
    render();
  }
  on(select, 'change', () => selectMonth(select.selectedIndex));
  on(root, 'click', e => {
    const btn = e.target.closest('button, rect[data-index]');
    if (!btn) { if (!tip.contains(e.target)) dismiss(); return; }
    if (btn.hasAttribute('data-retry')) { void load(); return; }
    if (btn.hasAttribute('data-index')) { selectMonth(Number(btn.dataset.index)); return; }
    if (btn.hasAttribute('data-entry')) {
      if (pinned && active === btn) dismiss(); else show(btn, true);
      return;
    }
    if (btn.hasAttribute('data-drill') && active) {
      parent = entries[Number(active.dataset.entry)].name;
      all = false;
      render();
      q('[data-back]').focus();
    } else if (btn.hasAttribute('data-key')) {
      metric = metrics.find(item => item.key === btn.dataset.key);
      parent = null;
      all = false;
      render();
      const selected = q('[data-key="' + metric.key + '"]');
      selected.focus({preventScroll:true});
      show(selected, e.pointerType === 'touch');
    } else if (btn.hasAttribute('data-back')) {
      parent = null;
      all = false;
      render();
    } else if (btn.hasAttribute('data-more')) {
      all = !all;
      render();
    }
  });
  const target = e => e.target.closest('[data-entry], [data-key]');
  on(root, 'pointerover', e => {
    if (e.pointerType === 'mouse' && !pinned && target(e) && target(e) !== active) show(target(e));
  });
  on(root, 'pointerout', e => {
    if (!pinned && !tip.contains(e.relatedTarget) && !active?.contains(e.relatedTarget)) dismiss();
  });
  on(root, 'focusin', e => { if (target(e) && !pinned) show(target(e)); });
  on(root, 'focusout', e => { if (!root.contains(e.relatedTarget)) dismiss(); });
  on(root, 'keydown', e => { if (e.key === 'Escape') dismiss(); });
  on(root.ownerDocument, 'pointerdown', e => { if (!root.contains(e.target)) dismiss(); });
  const observer = new win.ResizeObserver(drawTrend);
  observer.observe(svg);
  async function load() {
    if (loading || months.length) return;
    loading = true;
    q('[data-retry]').hidden = true;
    try {
      const payload = await read();
      if (!isActive()) return;
      months = payload.months;
      if (!months?.length) throw new Error('No completed month');
      select.replaceChildren(...months.map(month => new Option(month.date.slice(0, 7), month.m)));
      q('[data-report]').hidden = false;
      q('.monthlyStatus').hidden = true;
      selectMonth(months.length - 1);
    } catch {
      if (isActive()) {
        q('.monthlyStatus').textContent = '月末对账暂未读到';
        q('[data-retry]').hidden = false;
      }
    } finally { loading = false; }
  }
  return {load, dispose() { listeners.abort(); observer.disconnect(); }};
}
