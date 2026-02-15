import { jget, fmt2, accountLabel } from './api.js';
import { setActiveNav } from './ui.js';
import { pnlPct, safeNum, showError, createLineChart } from './utils.js';

const CONFIGS = {
  'A股': {
    title: 'A股持仓',
    apiPath: '/holdings/by-category/A股',
    chartApi: '/holdings/history/A股',
    chartColor: '#b33b3b',
    chartLabel: 'A股市值',
    interval: 'daily',
    tableTitle: 'A股持仓明细',
    chartTitle: 'A股市值走势（近90天）',
    note: '',
    headHtml: `<tr>
      <th class="nowrap" style="width:100px">代码</th>
      <th style="width:220px">名称</th>
      <th class="nowrap num" style="width:90px">买入价格</th>
      <th class="nowrap num" style="width:80px">数量</th>
      <th class="nowrap num" style="width:90px">当前价格</th>
      <th class="nowrap num" style="width:110px">持仓市值</th>
      <th class="nowrap num" style="width:110px">盈亏比</th>
    </tr>`,
    renderRow(r) {
      const pct = pnlPct(r);
      return `
        <td class="nowrap">${r.symbol || '—'}</td>
        <td class="name">${r.name || ''}</td>
        <td class="mono num">${r.buy_price ? fmt2(r.buy_price) : '—'}</td>
        <td class="mono num">${r.quantity ? fmt2(r.quantity) : '—'}</td>
        <td class="mono num">${r.current_price ? fmt2(r.current_price) : '—'}</td>
        <td class="mono num">${fmt2(r.market_value)}</td>
        <td class="mono num" style="color:${pct >= 0 ? 'var(--good)' : 'var(--bad)'}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</td>
      `;
    }
  },
  '美股': {
    title: '美股持仓',
    apiPath: '/holdings/by-category/美股',
    chartApi: '/holdings/history/美股',
    chartColor: '#c9a84c',
    chartLabel: '美股市值',
    interval: 'daily',
    tableTitle: '美股持仓明细',
    chartTitle: '美股市值走势（近90天）',
    note: '',
    headHtml: `<tr>
      <th class="nowrap" style="width:100px">代码</th>
      <th style="width:220px">名称</th>
      <th class="nowrap num" style="width:90px">买入价格</th>
      <th class="nowrap num" style="width:80px">数量</th>
      <th class="nowrap num" style="width:90px">当前价格</th>
      <th class="nowrap num" style="width:110px">持仓市值</th>
      <th class="nowrap num" style="width:110px">盈亏比</th>
    </tr>`,
    renderRow(r) {
      const pct = pnlPct(r);
      return `
        <td class="nowrap">${r.symbol || '—'}</td>
        <td class="name">${r.name || ''}</td>
        <td class="mono num">${r.buy_price ? fmt2(r.buy_price) : '—'}</td>
        <td class="mono num">${r.quantity ? fmt2(r.quantity) : '—'}</td>
        <td class="mono num">${r.current_price ? fmt2(r.current_price) : '—'}</td>
        <td class="mono num">${fmt2(r.market_value)}</td>
        <td class="mono num" style="color:${pct >= 0 ? 'var(--good)' : 'var(--bad)'}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</td>
      `;
    }
  },
  '加密': {
    title: '加密持仓',
    apiPath: '/holdings/by-category/加密',
    chartApi: '/holdings/history/加密',
    chartColor: '#8a6d3b',
    chartLabel: '加密资产市值',
    interval: 'daily',
    tableTitle: '加密持仓明细',
    chartTitle: '加密资产市值走势（近90天）',
    note: '',
    headHtml: `<tr>
      <th style="width:260px">名称</th>
      <th class="nowrap num" style="width:100px">买入价格</th>
      <th class="nowrap num" style="width:90px">数量</th>
      <th class="nowrap num" style="width:100px">当前价格</th>
      <th class="nowrap num" style="width:120px">持仓市值</th>
      <th class="nowrap num" style="width:120px">盈亏比</th>
    </tr>`,
    renderRow(r) {
      const pct = pnlPct(r);
      return `
        <td class="name">${r.name || ''}</td>
        <td class="mono num">${r.buy_price ? fmt2(r.buy_price) : '—'}</td>
        <td class="mono num">${r.quantity ? fmt2(r.quantity) : '—'}</td>
        <td class="mono num">${r.current_price ? fmt2(r.current_price) : '—'}</td>
        <td class="mono num">${fmt2(r.market_value)}</td>
        <td class="mono num" style="color:${pct >= 0 ? 'var(--good)' : 'var(--bad)'}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</td>
      `;
    }
  },
  '贵金属': {
    title: '贵金属持仓',
    apiPath: '/holdings/by-category/贵金属',
    chartApi: '/holdings/history/贵金属',
    chartColor: '#d4a24e',
    chartLabel: '贵金属市值',
    interval: 'daily',
    tableTitle: '贵金属持仓明细',
    chartTitle: '贵金属市值走势（近90天）',
    note: '',
    headHtml: `<tr>
      <th style="width:260px">名称</th>
      <th class="nowrap num" style="width:100px">买入价格</th>
      <th class="nowrap num" style="width:90px">数量</th>
      <th class="nowrap num" style="width:100px">当前价格</th>
      <th class="nowrap num" style="width:120px">持仓市值</th>
      <th class="nowrap num" style="width:120px">盈亏比</th>
    </tr>`,
    renderRow(r) {
      const pct = pnlPct(r);
      return `
        <td class="name">${r.name || ''}</td>
        <td class="mono num">${r.buy_price ? fmt2(r.buy_price) : '—'}</td>
        <td class="mono num">${r.quantity ? fmt2(r.quantity) : '—'}</td>
        <td class="mono num">${r.current_price ? fmt2(r.current_price) : '—'}</td>
        <td class="mono num">${fmt2(r.market_value)}</td>
        <td class="mono num" style="color:${pct >= 0 ? 'var(--good)' : 'var(--bad)'}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</td>
      `;
    }
  },
  '其他': {
    title: '其他持仓',
    apiPath: '/holdings/by-category/其他',
    chartApi: '/holdings/history/其他',
    chartColor: '#6b5b4e',
    chartLabel: '其他资产市值',
    interval: 'weekly',
    tableTitle: '其他持仓明细',
    chartTitle: '其他资产市值走势（近90天，按周）',
    note: '包含理财、现金、公积金、借款、欠款',
    headHtml: `<tr>
      <th class="nowrap" style="width:120px">账户</th>
      <th class="nowrap" style="width:80px">小类</th>
      <th style="width:260px">名称</th>
      <th class="nowrap" style="width:60px">币种</th>
      <th class="nowrap num" style="width:120px">市值</th>
      <th class="nowrap num" style="width:120px">盈亏比</th>
      <th class="nowrap num" style="width:100px">昨日收益</th>
    </tr>`,
    renderRow(r) {
      const pct = pnlPct(r);
      return `
        <td class="nowrap">${accountLabel(r.account)}</td>
        <td class="nowrap">${r.minor_category || ''}</td>
        <td class="name">${r.name || ''}</td>
        <td class="nowrap">${r.currency || ''}</td>
        <td class="mono num">${fmt2(r.market_value)}</td>
        <td class="mono num" style="color:${pct >= 0 ? 'var(--good)' : 'var(--bad)'}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</td>
        <td class="mono num" style="color:${safeNum(r.daily_pnl) >= 0 ? 'var(--good)' : 'var(--bad)'}">${fmt2(r.daily_pnl)}</td>
      `;
    }
  }
};

function renderRows(rows, renderRowFn) {
  const tbody = document.getElementById('rows');
  tbody.innerHTML = '';
  for (const r of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = renderRowFn(r);
    tbody.appendChild(tr);
  }
  document.getElementById('totalCount').textContent = rows.length;
}

async function main() {
  setActiveNav();

  const category = new URLSearchParams(location.search).get('category');
  const cfg = category ? CONFIGS[category] : null;

  if (!cfg) {
    showError('未知分类：' + (category || '(空)') + '。支持：A股、美股、加密、贵金属、其他');
    return;
  }

  document.title = 'PAMS · ' + cfg.title;
  document.getElementById('pageTitle').textContent = cfg.title;
  document.getElementById('chartTitle').textContent = cfg.chartTitle;
  document.getElementById('tableTitle').textContent = cfg.tableTitle;
  document.getElementById('tableHead').innerHTML = cfg.headHtml;
  document.getElementById('tableNote').textContent = cfg.note;

  try {
    const holdings = await jget(cfg.apiPath + '?limit=500');
    document.getElementById('asof').textContent = holdings.as_of || '--';
    renderRows(holdings.rows || [], cfg.renderRow);
    await createLineChart('trendChart', cfg.chartApi, cfg.chartColor, cfg.chartLabel, cfg.interval);
  } catch (err) {
    showError('加载失败：' + String(err));
  }
}

main();
