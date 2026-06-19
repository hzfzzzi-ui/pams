import { jget, accountLabel, fmtMoneyCompact, fmtSignedMoneyCompact } from './api.js?v=20260606-refresh-v3';
import { setActiveNav } from './ui.js?v=20260606-refresh-v3';
import { safeNum } from './utils.js?v=20260606-refresh-v3';

let fxRatesByBase = {};
let fxReversed = false;

const INVESTMENT_CLASSES = ['A股', '美股', 'ESOP', '贵金属'];

async function main(){
  setActiveNav();

  const [
    summary,
    valuationState,
    navResp,
    accountsResp,
    investmentSeries,
    confirmedInvestment,
    fxCNY,
    fxUSD,
    fxMXN,
    riskResp,
    holdingPreview,
  ] = await Promise.all([
    jget('/summary2/latest?baseCurrency=CNY'),
    jget('/api/portfolio/valuation-state?baseCurrency=CNY').catch(() => null),
    jget('/nav/weekly?start=2026-01-01&baseCurrency=CNY&latest=true&interval=daily').catch(() => ({ rows: [] })),
    jget('/api/accounts/overview').catch(() => ({ accounts: [] })),
    jget('/investment/return-series?start=2026-01-01&interval=daily&baseCurrency=CNY').catch(() => ({ rows: [] })),
    loadConfirmedInvestmentReturn().catch(() => null),
    jget('/api/fx-rates/latest?base_currency=CNY').catch(() => ({ rates: [] })),
    jget('/api/fx-rates/latest?base_currency=USD').catch(() => ({ rates: [] })),
    jget('/api/fx-rates/latest?base_currency=MXN').catch(() => ({ rates: [] })),
    jget('/risk/latest?limit=10').catch(() => ({ rows: [] })),
    loadDashboardHoldingsPreview().catch(() => []),
  ]);

  const asOf = valuationState?.as_of || summary.as_of || navResp.end || '--';
  document.getElementById('asof').textContent = `${asOf}（${valuationState?.base_currency || summary.base_currency || navResp.base_currency || 'CNY'}）`;
  document.getElementById('api').textContent = `FastAPI ${location.hostname}:8090`;

  const byClass = summary.by_asset_class || [];
  const children = summary.children || [];
  const navRows = getSeriesRows(navResp);
  const rawLatestNav = navRows.length ? navRows[navRows.length - 1] : null;
  const latestNav = valuationState?.net_liquidation_value != null
    ? {
      asOfDate: valuationState.as_of || valuationState.valuation_date || rawLatestNav?.asOfDate,
      assets: safeNum(valuationState.assets_cny),
      liabilities: safeNum(valuationState.liabilities_cny),
      netWorth: safeNum(valuationState.net_liquidation_value),
    }
    : rawLatestNav;
  const prevNav = navRows.length > 1 ? navRows[navRows.length - 2] : null;
  let total = latestNav ? latestNav.assets : 0;
  let liab = latestNav ? latestNav.liabilities : 0;

  if (!latestNav) {
    for (const item of children) {
      const mv = safeNum(item.market_value);
      if (item.asset_class === 'LIABILITY') {
        liab += Math.abs(mv);
      } else if (item.asset_class !== 'IGNORE') {
        total += mv;
      }
    }
  }

  const net = latestNav ? latestNav.netWorth : total - liab;
  const accountStructure = buildAccountStructure(accountsResp.accounts || [], fxCNY.rates || []);
  const investmentRows = investmentSeries.rows || [];
  const latestInvestment = investmentRows.length ? investmentRows[investmentRows.length - 1] : null;
  const investmentReturn = latestInvestment
    ? safeNum(latestInvestment.total_pnl)
    : confirmedInvestment
    ? safeNum(confirmedInvestment.total_pnl)
    : byClass
      .filter(cls => INVESTMENT_CLASSES.includes(cls.asset_class))
      .reduce((sum, cls) => sum + safeNum(cls.holding_pnl), 0);
  const dailyChange = safeNum(summary.totals?.daily_pnl);
  const cashAvailable = availableCashFromAccounts(accountStructure, byClass);
  const investmentTodayReturn = todayInvestmentReturnFromSeries(investmentSeries);
  renderDashboardPrimarySummary({ asOf, total, liab, net, investmentTodayReturn, cashAvailable });
  renderDashboardKpis({ net, investmentTodayReturn, cashAvailable });
  renderBalanceFormula(total, liab, net);
  renderNetWorthBridge({ prevNav, latestNav, net, dailyChange });
  renderDashboardHoldingPreview(holdingPreview);

  fxRatesByBase = {
    CNY: fxCNY.rates || [],
    USD: fxUSD.rates || [],
    MXN: fxMXN.rates || [],
  };
  renderFxRates(fxCNY.as_of);
  document.getElementById('fxToggleBtn')?.addEventListener('click', () => {
    fxReversed = !fxReversed;
    renderFxRates(fxCNY.as_of);
  });

  const alerts = (riskResp.rows || []).map(r => ({
    severity: r.severity,
    title: r.title || r.message || r.code,
    details: r.details,
  }));
  renderDashboardAlerts(document.getElementById('alerts'), alerts);

  if (accountStructure.assets.length || accountStructure.liabilities.length) {
    renderAccountAssetStructure(accountStructure.assets);
    renderAccountLiabilityStructure(accountStructure.liabilities);
  } else {
    renderAssetStructure(byClass, children);
    renderLiabilityStructure(children);
  }
  renderNavChart(navResp);
  renderMDDFromNav(navResp);
  renderAssetLiabilityTrend(navResp);
  renderInvestmentPnl(byClass, confirmedInvestment, accountStructure, latestInvestment);
}

function renderFxRates(asOfDate) {
  const fxBody = document.getElementById('fxBody');
  if (!fxBody) return;

  const targetCurrencies = ['USD', 'MXN'];
  const rows = [];

  if (!fxReversed) {
    const cnyRates = fxRatesByBase.CNY || [];
    for (const currency of targetCurrencies) {
      const rateObj = cnyRates.find(r => r.quote_currency === currency);
      if (rateObj) {
        rows.push(`<div class="fxRow"><span class="fxPair">${currency}/CNY</span><span class="fxRate">${safeNum(rateObj.rate).toFixed(4)}</span></div>`);
      }
    }
  } else {
    for (const currency of targetCurrencies) {
      const baseRates = fxRatesByBase[currency] || [];
      const rateObj = baseRates.find(r => r.quote_currency === 'CNY');
      if (rateObj) {
        rows.push(`<div class="fxRow"><span class="fxPair">CNY/${currency}</span><span class="fxRate">${safeNum(rateObj.rate).toFixed(4)}</span></div>`);
      }
    }
  }

  if (asOfDate) rows.push(`<div class="muted" style="margin-top:4px;font-size:10px">${asOfDate}</div>`);
  fxBody.innerHTML = rows.length ? rows.join('') : '<div class="muted">暂无汇率数据</div>';
}

function metric(label, value, help = '', tone = '') {
  const title = String(help || label).replace(/"/g, '&quot;');
  return `
    <div class="metricTile ${tone}" title="${title}">
      <div class="metricLabel">${label}</div>
      <div class="metricValue mono">${value}</div>
      <div class="metricHelp">${help || '—'}</div>
    </div>
  `;
}

function primaryStat(label, value, tone = '') {
  return `<div class="mobileHeroStat"><span>${label}</span><strong class="mono ${tone}">${value}</strong></div>`;
}

function renderDashboardPrimarySummary({ asOf, total, liab, net, investmentTodayReturn, cashAvailable }) {
  const el = document.getElementById('dashboardPrimarySummary');
  if (!el) return;
  const liabilityRatio = total > 0 ? liab / total * 100 : 0;
  const returnTone = investmentTodayReturn >= 0 ? 'up' : 'down';
  const netTone = net >= 0 ? 'up' : 'down';
  el.innerHTML = `
    <div class="mobileHeroLabel">净资产</div>
    <div class="mobileHeroValue mono ${netTone}">${fmtMoneyCompact(net)}</div>
    <div class="mobileHeroStats">
      ${primaryStat('今日收益', fmtSignedMoneyCompact(investmentTodayReturn), returnTone)}
      ${primaryStat('可用现金', fmtMoneyCompact(cashAvailable))}
      ${primaryStat('总资产', fmtMoneyCompact(total))}
      ${primaryStat('负债率', `${liabilityRatio.toFixed(1)}%`, liabilityRatio > 35 ? 'down' : '')}
    </div>
    <div class="mobileHeroMeta">
      <span>${asOf || '--'} · CNY</span>
    </div>
  `;
}

function renderDashboardKpis({ net, investmentTodayReturn, cashAvailable }) {
  const kpiEl = document.getElementById('kpis');
  if (!kpiEl) return;
  kpiEl.innerHTML = [
    metric('净资产', fmtMoneyCompact(net), '资产负债净额', net >= 0 ? 'good' : 'bad'),
    metric('今日收益', fmtSignedMoneyCompact(investmentTodayReturn), '北京时间 15:05 收益窗口', investmentTodayReturn >= 0 ? 'good' : 'bad'),
    metric('可用现金', fmtMoneyCompact(cashAvailable), '现金与支付账户可用余额', cashAvailable > 0 ? '' : 'warn'),
  ].join('');
}

function todayInvestmentReturnFromSeries(resp) {
  if (resp?.today_window?.total_return != null) return safeNum(resp.today_window.total_return);
  const rows = resp?.rows || [];
  if (!rows.length) return 0;
  const latest = rows[rows.length - 1];
  const previous = rows.length > 1 ? rows[rows.length - 2] : null;
  return previous ? safeNum(latest.total_pnl) - safeNum(previous.total_pnl) : 0;
}

function holdingValue(row) {
  return safeNum(row.market_value_cny ?? row.market_value ?? 0);
}

function holdingPnl(row) {
  return safeNum(row.holding_pnl_cny ?? row.holding_pnl ?? 0);
}

async function loadDashboardHoldingsPreview() {
  const a = encodeURIComponent('A股');
  const endpoints = [
    { label: 'A股个股', url: `/holdings/by-category/${a}?minor_category=${encodeURIComponent('A股')}&limit=500` },
    { label: '基金', url: `/holdings/by-category/${a}?minor_category=${encodeURIComponent('A股基金')}&limit=500` },
    { label: '美股/海外', url: `/holdings/by-category/${encodeURIComponent('美股')}?limit=500` },
    { label: '贵金属', url: `/holdings/by-category/${encodeURIComponent('贵金属')}?limit=500` },
    { label: '内部股份', url: `/holdings/by-category/${encodeURIComponent('ESOP')}?limit=500` },
  ];
  const responses = await Promise.all(endpoints.map(item => jget(item.url).catch(() => ({ rows: [] }))));
  return responses.flatMap((resp, index) => (resp.rows || []).map(row => ({
    name: row.name || row.symbol || '未命名资产',
    symbol: row.symbol || '',
    category: endpoints[index].label,
    value: holdingValue(row),
    pnl: holdingPnl(row),
  }))).filter(row => row.value > 0.005).sort((a, b) => b.value - a.value);
}

function renderDashboardHoldingPreview(rows) {
  const el = document.getElementById('dashboardHoldingPreview');
  if (!el) return;
  const topRows = (rows || []).slice(0, 6);
  if (!topRows.length) {
    el.innerHTML = '<div class="muted">暂无持仓数据</div>';
    return;
  }
  const total = rows.reduce((sum, row) => sum + safeNum(row.value), 0);
  el.innerHTML = topRows.map(row => {
      const ratio = total > 0 ? row.value / total : 0;
      const displayName = String(row.name || row.symbol).replace(/^(.+?) - /, '');
      return `<a class="dashboardHoldingRow" href="investments.html#holdings-smart-table" title="${displayName}">
        <span>
          <strong>${displayName}</strong>
          <small>${row.category} · ${(ratio * 100).toFixed(1)}%</small>
        </span>
        <em>
          <b class="mono">${fmtMoneyCompact(row.value)}</b>
          <small class="mono ${row.pnl >= 0 ? 'up' : 'down'}">${fmtSignedMoneyCompact(row.pnl)}</small>
        </em>
      </a>`;
    }).join('');
}

function renderBalanceFormula(total, liab, net) {
  const el = document.getElementById('balanceFormula');
  if (!el) return;
  el.innerHTML = `
    <span>总资产 <strong>${fmtMoneyCompact(total)}</strong></span>
    <span class="formulaOperator">−</span>
    <span>总负债 <strong>${fmtMoneyCompact(liab)}</strong></span>
    <span class="formulaOperator">=</span>
    <span>净资产 <strong>${fmtMoneyCompact(net)}</strong></span>
  `;
}

function renderNetWorthBridge({ prevNav, latestNav, net, dailyChange }) {
  const el = document.getElementById('netWorthBridge');
  if (!el) return;
  const prevNet = prevNav?.netWorth ?? net;
  const currentNet = latestNav?.netWorth ?? net;
  const residual = currentNet - prevNet - dailyChange;
  const rows = [
    ['上一采样净资产', fmtMoneyCompact(prevNet), 'data'],
    ['投资收益', fmtSignedMoneyCompact(dailyChange), dailyChange >= 0 ? 'good' : 'bad'],
    ['现金流/汇率/核准调整', fmtSignedMoneyCompact(residual), 'pending'],
    ['当前净资产', fmtMoneyCompact(currentNet), 'data'],
  ];
  el.innerHTML = rows.map(([label, value, tone], idx) => `
    <div class="bridgeItem ${tone}">
      <div class="bridgeLabel">${label}</div>
      <div class="bridgeValue mono">${value}</div>
      ${idx === 2 ? '<div class="bridgeHint">合并估算，需后续拆分</div>' : ''}
    </div>
  `).join('<div class="bridgeOp">→</div>');
}

function renderDashboardAlerts(el, alerts) {
  if (!el) return;
  if (!alerts?.length) {
    el.innerHTML = '<span class="miniBadge ok">暂无告警</span>';
    return;
  }
  el.innerHTML = alerts.slice(0, 6).map((alert) => {
    const sev = alert.severity || 'info';
    const tone = sev === 'risk' || sev === 'crit' ? 'warn' : 'data';
    return `<span class="miniBadge ${tone}">${alert.title || '风险提示'}</span>`;
  }).join('');
}

function availableCashFromAccounts(accountStructure, byClass) {
  const accountCash = (accountStructure?.assets || [])
    .filter(row => ['现金与支付', '在途资金'].includes(row.name))
    .reduce((sum, row) => sum + safeNum(row.value), 0);
  if (accountCash > 0.0001) return accountCash;
  return (byClass || [])
    .filter(row => row.asset_class === '现金')
    .reduce((sum, row) => sum + safeNum(row.market_value), 0);
}

function rowPnlCny(row) {
  return safeNum(row.holding_pnl_cny ?? row.holding_pnl ?? 0);
}

function rowValueCny(row) {
  return safeNum(row.market_value_cny ?? row.market_value ?? 0);
}

function rowCostCny(row) {
  return safeNum(row.cost_cny ?? row.cost ?? 0);
}

function reportedEntryKey(entry) {
  return entry?.symbol ? `symbol:${entry.symbol}` : `name:${entry?.name || ''}`;
}

function findReportedEntry(row, section) {
  const entries = section?.entries || [];
  const rowSymbol = String(row.symbol || '');
  const rowName = String(row.name || '');
  return entries.find(entry => (
    (entry.symbol && String(entry.symbol) === rowSymbol) ||
    (!entry.symbol && String(entry.name || '') === rowName)
  ));
}

function rowAccountCode(row) {
  return String(row.account_code || row.account || row.venue || '');
}

function reportedAdjustedPnl(rows, section, accountPerformance = {}) {
  if (!section?.entries?.length) {
    const countedAccounts = new Set();
    return (rows || []).reduce((sum, row) => {
      const code = rowAccountCode(row);
      const accountPerf = accountPerformance?.[code];
      if (accountPerf?.total_ytd_pnl != null) {
        if (countedAccounts.has(code)) return sum;
        countedAccounts.add(code);
        return sum + safeNum(accountPerf.total_ytd_pnl);
      }
      return sum + rowPnlCny(row);
    }, 0);
  }
  let pnl = 0;
  const represented = new Set();
  const countedAccounts = new Set();
  for (const row of rows || []) {
    const code = rowAccountCode(row);
    const accountPerf = accountPerformance?.[code];
    if (accountPerf?.total_ytd_pnl != null) {
      if (!countedAccounts.has(code)) {
        pnl += safeNum(accountPerf.total_ytd_pnl);
        countedAccounts.add(code);
      }
      continue;
    }
    const entry = findReportedEntry(row, section);
    if (entry) {
      pnl += safeNum(entry.ytd_pnl);
      represented.add(reportedEntryKey(entry));
    } else {
      pnl += rowPnlCny(row);
    }
  }
  if (section.total_ytd_pnl != null) {
    const representedPnl = (section.entries || [])
      .filter(entry => represented.has(reportedEntryKey(entry)))
      .reduce((sum, entry) => sum + safeNum(entry.ytd_pnl), 0);
    pnl += safeNum(section.total_ytd_pnl) - representedPnl;
  }
  return pnl;
}

async function loadConfirmedInvestmentReturn() {
  const [fundAll, usRows, precious, esop, ibkrPerf, aStockPerf, reported] = await Promise.all([
    jget('/holdings/by-category/A股?minor_category=A股基金&limit=500&include_closed=true'),
    jget('/holdings/by-category/美股?limit=500&include_closed=true'),
    jget('/holdings/by-category/贵金属?limit=500'),
    jget('/holdings/by-category/ESOP?limit=500&include_closed=true'),
    jget('/api/ibkr/performance').catch(() => null),
    jget('/api/account-performance/tonghuashun').catch(() => null),
    jget('/api/investment-returns/reported').catch(() => null),
  ]);
  const fundSection = reported?.sections?.a_share_fund;
  const preciousSection = reported?.sections?.precious_metals;
  const accountPerformance = reported?.account_performance || {};
  const byCategory = {
    'A股': safeNum(aStockPerf?.total_pnl_cny) + reportedAdjustedPnl(fundAll.rows || [], fundSection, accountPerformance),
    '美股': safeNum(ibkrPerf?.total_pnl_cny),
    '贵金属': (preciousSection?.total_ytd_pnl != null
      ? safeNum(preciousSection.total_ytd_pnl)
      : (precious.rows || []).reduce((sum, row) => sum + rowPnlCny(row), 0)),
    'ESOP': (esop.rows || []).reduce((sum, row) => sum + rowPnlCny(row), 0),
  };
  const aStockCost = safeNum(aStockPerf?.initial_capital_cny)
    || Math.max(safeNum(aStockPerf?.net_liquidation_cny) - safeNum(aStockPerf?.total_pnl_cny), 0);
  const fundCost = (fundAll.rows || []).reduce((sum, row) => {
    const entry = findReportedEntry(row, fundSection);
    return sum + safeNum(entry?.bank_cash_cost ?? rowCostCny(row));
  }, 0);
  const usValue = (usRows.rows || []).reduce((sum, row) => sum + rowValueCny(row), 0);
  const ibkrCost = Math.max(usValue - safeNum(ibkrPerf?.total_pnl_cny), 0);
  const preciousCost = (precious.rows || []).reduce((sum, row) => {
    const entry = findReportedEntry(row, preciousSection);
    return sum + safeNum(entry?.bank_cash_cost ?? rowCostCny(row));
  }, 0);
  const esopCost = (esop.rows || []).reduce((sum, row) => sum + rowCostCny(row), 0);
  const totalPnl = Object.values(byCategory).reduce((sum, value) => sum + safeNum(value), 0);
  const totalCost = aStockCost + fundCost + ibkrCost + preciousCost + esopCost;
  return { total_pnl: totalPnl, total_cost: totalCost, by_category: byCategory };
}

function latestCnyRate(currency, fxRates) {
  const c = String(currency || 'CNY').toUpperCase();
  if (c === 'CNY') return 1;
  const row = (fxRates || []).find(r => r.quote_currency === c);
  return row ? safeNum(row.rate) : 1;
}

function accountBucket(account, amountCny) {
  if (amountCny < 0 || account.account_type === 'liability') return '负债';
  if (account.account_type === 'brokerage' || account.account_type === 'fund_platform') return '投资资产';
  if (account.account_type === 'benefit') return '公积金与医保';
  if (account.account_type === 'receivable') return '应收款';
  if (account.account_type === 'bridge') return '在途资金';
  return '现金与支付';
}

function addAssetEntry(assetMap, bucket, label, value) {
  const entry = assetMap.get(bucket) || { name: bucket, value: 0, children: [] };
  entry.value += value;
  entry.children.push({ name: label, value });
  assetMap.set(bucket, entry);
}

function platformGrossSplit(account, fxRates) {
  const rows = account.platform_balances || [];
  if (!rows.length) return null;
  let assets = 0;
  let liabilities = 0;
  for (const row of rows) {
    const currency = row.currency || account.base_currency || 'CNY';
    const valueCny = safeNum(row.market_value) * latestCnyRate(currency, fxRates);
    const side = row.asset_liability || '';
    if (side === '负债' || valueCny < 0) {
      liabilities += Math.abs(valueCny);
    } else {
      assets += Math.abs(valueCny);
    }
  }
  return liabilities > 0 ? { assets, liabilities } : null;
}

function buildAccountStructure(accounts, fxRates) {
  const assetMap = new Map();
  const liabilityMap = new Map();
  for (const account of accounts || []) {
    if (!account.include_in_nav || !account.primary_balance) continue;
    const balance = account.primary_balance;
    const amount = safeNum(balance.amount);
    const currency = balance.currency || account.base_currency || 'CNY';
    const amountCny = amount * latestCnyRate(currency, fxRates);
    if (Math.abs(amountCny) < 0.0001) continue;
    const bucket = accountBucket(account, amountCny);
    const label = account.subaccount_label || account.display_name || account.account_code;
    const grossSplit = platformGrossSplit(account, fxRates);
    if (grossSplit) {
      if (grossSplit.assets > 0.0001) {
        addAssetEntry(assetMap, accountBucket(account, grossSplit.assets), label, grossSplit.assets);
      }
      if (grossSplit.liabilities > 0.0001) {
        liabilityMap.set(`${label}融资负债`, (liabilityMap.get(`${label}融资负债`) || 0) + grossSplit.liabilities);
      }
      continue;
    }
    if (amountCny < 0 || bucket === '负债') {
      liabilityMap.set(label, (liabilityMap.get(label) || 0) + Math.abs(amountCny));
    } else {
      addAssetEntry(assetMap, bucket, label, amountCny);
    }
  }
  return {
    assets: [...assetMap.values()].sort((a, b) => b.value - a.value),
    liabilities: [...liabilityMap.entries()].map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value),
  };
}

function rankedBarRow(label, value, total, subHtml = '', tone = '') {
  const pct = total > 0 ? value / total : 0;
  const width = Math.max(Math.min(pct * 100, 100), value > 0 ? 2 : 0);
  return `
    <details class="rankedBarRow ${tone}">
      <summary>
      <div class="rankedBarHead">
        <span class="rankedLabel">${label}</span>
        <span class="rankedAmount mono">${fmtMoneyCompact(value)} · ${(pct * 100).toFixed(1)}%</span>
      </div>
      <div class="rankedTrack"><div class="rankedFill" style="width:${width}%"></div></div>
      </summary>
      ${subHtml ? `<div class="rankedSub">${subHtml}</div>` : ''}
    </details>
  `;
}

function renderAccountAssetStructure(rows) {
  const byTypeEl = document.getElementById('byType');
  const pieEl = document.getElementById('pie');
  if (!byTypeEl) return;

  const totalAssets = rows.reduce((sum, r) => sum + safeNum(r.value), 0);
  byTypeEl.innerHTML = rows.map(r => {
    const subs = (r.children || [])
      .sort((a, b) => b.value - a.value)
      .slice(0, 3)
      .map(s => `<span>${s.name} ${fmtMoneyCompact(s.value)}</span>`)
      .join('');
    return rankedBarRow(r.name, r.value, totalAssets, subs);
  }).join('') || '<div class="muted">暂无数据</div>';

  if (pieEl && rows.length && chartAvailable()) {
    if (window.__pie) window.__pie.destroy();
    window.__pie = new Chart(pieEl, {
      type: 'doughnut',
      data: {
        labels: rows.map(x => x.name),
        datasets: [{
          data: rows.map(x => Number(x.value)),
          backgroundColor: ['#58a6ff','#3fb950','#d29922','#bc8cff','#f85149','#6e7681','#1f6feb'],
          borderColor: '#161a1e',
          borderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { position: 'bottom', labels: { color: '#e1e4e8', font: { size: 11 }, padding: 8 } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: ${fmtMoneyCompact(ctx.raw)}` } },
        },
      },
    });
  }
}

function renderAccountLiabilityStructure(rows) {
  const liabListEl = document.getElementById('liabilityList');
  const liabEl = document.getElementById('liabilityPie');
  if (!liabListEl) return;
  if (!rows.length) {
    liabListEl.innerHTML = '<div class="muted">暂无负债</div>';
    if (liabEl && window.__liab) window.__liab.destroy();
    return;
  }
  const totalLiab = rows.reduce((sum, r) => sum + safeNum(r.value), 0);
  liabListEl.innerHTML = rows.map(r => {
    return rankedBarRow(r.name, r.value, totalLiab, '', 'bad');
  }).join('');
  if (liabEl && chartAvailable()) {
    if (window.__liab) window.__liab.destroy();
    window.__liab = new Chart(liabEl, {
      type: 'doughnut',
      data: {
        labels: rows.map(e => e.name),
        datasets: [{
          data: rows.map(e => e.value),
          backgroundColor: ['#f85149','#d29922','#bc8cff','#58a6ff','#6e7681'],
          borderColor: '#161a1e',
          borderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { position: 'bottom', labels: { color: '#e1e4e8', font: { size: 11 }, padding: 8 } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: ${fmtMoneyCompact(ctx.raw)}` } },
        },
      },
    });
  }
}

function renderAssetStructure(byClass, children) {
  const byTypeEl = document.getElementById('byType');
  if (!byTypeEl) return;

  const assetClasses = byClass.filter(c => c.asset_class !== 'LIABILITY' && c.asset_class !== 'IGNORE');
  const byBig = new Map();
  const byBigSmall = new Map();

  for (const cls of assetClasses) {
    const big = cls.asset_class || '其他';
    const mv = safeNum(cls.market_value);
    byBig.set(big, (byBig.get(big) || 0) + mv);

    for (const venue of cls.by_venue || []) {
      const venueName = accountLabel(venue.venue) || venue.venue || 'Unknown';
      const venueMv = safeNum(venue.market_value);
      const key = `${big} > ${venueName}`;
      byBigSmall.set(key, (byBigSmall.get(key) || 0) + venueMv);
    }
  }

  const bigRows = [...byBig.entries()].map(([k, v]) => ({ k, v })).sort((a, b) => b.v - a.v);
  const totalAssets = bigRows.reduce((sum, row) => sum + safeNum(row.v), 0);
  const smallByBig = new Map();
  for (const [k, v] of byBigSmall.entries()) {
    const [big] = k.split(' > ');
    if (!smallByBig.has(big)) smallByBig.set(big, []);
    smallByBig.get(big).push({ k, v });
  }

  byTypeEl.innerHTML = bigRows.map(r => {
    const subs = (smallByBig.get(r.k) || [])
      .sort((a, b) => b.v - a.v)
      .slice(0, 3)
      .map(s => `<span>${s.k.split(' > ')[1]} ${fmtMoneyCompact(s.v)}</span>`)
      .join('');
    return rankedBarRow(r.k, r.v, totalAssets, subs);
  }).join('') || '<div class="muted">暂无数据</div>';

  const pieEl = document.getElementById('pie');
  if (pieEl && bigRows.length && chartAvailable()) {
    if (window.__pie) window.__pie.destroy();
    window.__pie = new Chart(pieEl, {
      type: 'doughnut',
      data: {
        labels: bigRows.map(x => x.k),
        datasets: [{
          data: bigRows.map(x => Number(x.v)),
          backgroundColor: ['#f85149','#58a6ff','#bc8cff','#d29922','#3fb950','#6e7681','#da3633','#1f6feb'],
          borderColor: '#161a1e',
          borderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { position: 'bottom', labels: { color: '#e1e4e8', font: { size: 11 }, padding: 8 } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: ${fmtMoneyCompact(ctx.raw)}` } },
        },
      },
    });
  }
}

function renderLiabilityStructure(children) {
  const liabListEl = document.getElementById('liabilityList');
  const liabEl = document.getElementById('liabilityPie');
  if (!liabListEl) return;

  const liabilities = children.filter(c => c.asset_class === 'LIABILITY');
  if (!liabilities.length) {
    liabListEl.innerHTML = '<div class="muted">暂无负债</div>';
    if (liabEl && window.__liab) window.__liab.destroy();
    return;
  }

  const liabMap = new Map();
  for (const item of liabilities) {
    const name = item.name || accountLabel(item.account) || item.account || 'Unknown';
    const value = Math.abs(safeNum(item.market_value));
    liabMap.set(name, (liabMap.get(name) || 0) + value);
  }

  const entries = [...liabMap.entries()].sort((a, b) => b[1] - a[1]);
  const totalLiab = entries.reduce((s, e) => s + e[1], 0);

  liabListEl.innerHTML = entries.map(([name, value]) => {
    return rankedBarRow(name, value, totalLiab, '', 'bad');
  }).join('');

  if (liabEl && chartAvailable()) {
    if (window.__liab) window.__liab.destroy();
    window.__liab = new Chart(liabEl, {
      type: 'doughnut',
      data: {
        labels: entries.map(e => e[0]),
        datasets: [{
          data: entries.map(e => e[1]),
          backgroundColor: ['#f85149','#58a6ff','#bc8cff','#d29922','#3fb950','#6e7681','#da3633'],
          borderColor: '#161a1e',
          borderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { position: 'bottom', labels: { color: '#e1e4e8', font: { size: 11 }, padding: 8 } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: ${fmtMoneyCompact(ctx.raw)}` } },
        },
      },
    });
  }
}

function getSeriesRows(navResp) {
  return (navResp.rows || []).map(r => ({
    date: r.date || r.asOfDate,
    assets: safeNum(r.assets),
    liabilities: safeNum(r.liabilities),
    netWorth: safeNum(r.netWorth),
  })).filter(r => r.date);
}

function chartAvailable() {
  return typeof window.Chart === 'function';
}

function fallbackLineChart(canvas, { labels, datasets, fill = false }) {
  const ctx = canvas.getContext('2d');
  const parent = canvas.parentElement;
  const rect = parent?.getBoundingClientRect?.() || canvas.getBoundingClientRect();
  const width = Math.max(280, Math.floor(rect.width || 320));
  const height = Math.max(180, Math.floor(rect.height || 220));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const allValues = datasets.flatMap(dataset => dataset.data).filter(Number.isFinite);
  if (!labels.length || !allValues.length) {
    ctx.fillStyle = '#6f7888';
    ctx.font = '12px sans-serif';
    ctx.fillText('暂无数据', 16, 28);
    return {
      fallback: true,
      data: { labels, datasets },
      destroy: () => ctx.clearRect(0, 0, width, height),
    };
  }

  let min = Math.min(...allValues);
  let max = Math.max(...allValues);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const pad = { left: 52, right: 18, top: 22, bottom: 34 };
  const plotW = Math.max(1, width - pad.left - pad.right);
  const plotH = Math.max(1, height - pad.top - pad.bottom);
  const xAt = index => pad.left + (labels.length <= 1 ? plotW : (index / (labels.length - 1)) * plotW);
  const yAt = value => pad.top + ((max - value) / (max - min)) * plotH;

  ctx.strokeStyle = 'rgba(255,255,255,.10)';
  ctx.lineWidth = 1;
  ctx.font = '11px sans-serif';
  ctx.fillStyle = '#6f7888';
  for (let i = 0; i <= 3; i += 1) {
    const y = pad.top + (plotH / 3) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    const value = max - ((max - min) / 3) * i;
    ctx.fillText(fmtMoneyCompact(value), 6, y + 4);
  }

  ctx.fillStyle = '#6f7888';
  ctx.fillText(labels[0], pad.left, height - 12);
  if (labels.length > 1) {
    const last = labels[labels.length - 1];
    ctx.textAlign = 'right';
    ctx.fillText(last, width - pad.right, height - 12);
    ctx.textAlign = 'left';
  }

  datasets.forEach((dataset, datasetIndex) => {
    const color = dataset.borderColor || '#58a6ff';
    ctx.beginPath();
    dataset.data.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = dataset.borderWidth || 2;
    ctx.stroke();

    if (fill && datasetIndex === 0) {
      ctx.lineTo(xAt(dataset.data.length - 1), pad.top + plotH);
      ctx.lineTo(xAt(0), pad.top + plotH);
      ctx.closePath();
      ctx.fillStyle = dataset.backgroundColor || 'rgba(88,166,255,.08)';
      ctx.fill();
    }

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(width - pad.right - 96, pad.top + datasetIndex * 17, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#dbe4f0';
    ctx.fillText(dataset.label || '', width - pad.right - 86, pad.top + 4 + datasetIndex * 17);
  });

  return {
    fallback: true,
    data: { labels, datasets },
    destroy: () => ctx.clearRect(0, 0, width, height),
  };
}

function renderNavChart(navResp) {
  const navEl = document.getElementById('navChart');
  if (!navEl) return;

  const series = getSeriesRows(navResp);
  const labels = series.map(x => x.date);
  const data = series.map(x => x.netWorth);

  if (window.__nav) window.__nav.destroy();
  if (!chartAvailable()) {
    window.__nav = fallbackLineChart(navEl, {
      labels,
      fill: true,
      datasets: [{
        label: '净资产',
        data,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,.10)',
        borderWidth: 2,
      }],
    });
    return;
  }
  window.__nav = new Chart(navEl, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: '净资产（CNY）',
        data,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,.08)',
        tension: 0.25,
        fill: true,
        pointRadius: labels.length > 30 ? 0 : 2,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => fmtMoneyCompact(ctx.raw) } },
      },
      scales: {
        x: { ticks: { color: '#6e7681', maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: 'rgba(110,118,129,.12)' } },
        y: { ticks: { color: '#6e7681', callback: v => fmtMoneyCompact(v) }, grid: { color: 'rgba(110,118,129,.12)' } },
      },
    },
  });
}

function renderAssetLiabilityTrend(navResp) {
  const el = document.getElementById('catNavChart');
  if (!el) return;

  const series = getSeriesRows(navResp);
  const labels = series.map(x => x.date);

  if (window.__catNav) window.__catNav.destroy();
  const fallbackDatasets = [
    { label: '总资产', data: series.map(x => x.assets), borderColor: '#3fb950', backgroundColor: 'rgba(63,185,80,.08)', borderWidth: 2 },
    { label: '总负债', data: series.map(x => x.liabilities), borderColor: '#f85149', backgroundColor: 'rgba(248,81,73,.08)', borderWidth: 2 },
    { label: '净资产', data: series.map(x => x.netWorth), borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,.08)', borderWidth: 2 },
  ];
  if (!chartAvailable()) {
    window.__catNav = fallbackLineChart(el, { labels, datasets: fallbackDatasets });
    return;
  }
  window.__catNav = new Chart(el, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: '总资产', data: series.map(x => x.assets), borderColor: '#3fb950', backgroundColor: 'rgba(63,185,80,.08)', tension: 0.25, fill: false, borderWidth: 2, pointRadius: labels.length > 30 ? 0 : 2 },
        { label: '总负债', data: series.map(x => x.liabilities), borderColor: '#f85149', backgroundColor: 'rgba(248,81,73,.08)', tension: 0.25, fill: false, borderWidth: 2, pointRadius: labels.length > 30 ? 0 : 2 },
        { label: '净资产', data: series.map(x => x.netWorth), borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,.08)', tension: 0.25, fill: false, borderWidth: 2, pointRadius: labels.length > 30 ? 0 : 2 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      spanGaps: true,
      plugins: {
        legend: { position: 'top', labels: { color: '#e1e4e8', font: { size: 11 }, padding: 10, usePointStyle: true } },
        tooltip: { mode: 'index', intersect: false, callbacks: { label: ctx => `${ctx.dataset.label}: ${fmtMoneyCompact(ctx.raw)}` } },
      },
      scales: {
        x: { ticks: { color: '#6e7681', maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: 'rgba(110,118,129,.12)' } },
        y: { ticks: { color: '#6e7681', callback: v => fmtMoneyCompact(v) }, grid: { color: 'rgba(110,118,129,.12)' } },
      },
    },
  });
}

function renderMDDFromNav(navResp) {
  const badge = document.getElementById('mddBadge');
  if (!badge) return;

  let peak = null;
  let mdd = 0;
  for (const row of getSeriesRows(navResp)) {
    const net = row.netWorth;
    if (net <= 0) continue;
    peak = peak == null ? net : Math.max(peak, net);
    if (peak > 0) mdd = Math.min(mdd, net / peak - 1);
  }

  badge.textContent = `MDD: ${(mdd * 100).toFixed(2)}%`;
}

function investmentAssetValueFromAccounts(accountStructure) {
  const investmentBucket = (accountStructure?.assets || []).find(row => row.name === '投资资产');
  return investmentBucket ? safeNum(investmentBucket.value) : null;
}

function renderInvestmentPnl(byClass, confirmedInvestment = null, accountStructure = null, latestInvestment = null) {
  const el = document.getElementById('holdingsAnalysis');
  if (!el) return;

  const assetClasses = byClass.filter(c => c.asset_class !== 'LIABILITY' && c.asset_class !== 'IGNORE');
  const investmentClasses = assetClasses.filter(c => INVESTMENT_CLASSES.includes(c.asset_class));
  const confirmedByCategory = confirmedInvestment?.by_category || {};

  const classInvestmentValue = investmentClasses.reduce((sum, cls) => sum + safeNum(cls.market_value), 0);
  const investmentValue = investmentAssetValueFromAccounts(accountStructure) ?? classInvestmentValue;
  const investmentCashValue = Math.max(investmentValue - classInvestmentValue, 0);
  const investmentPnl = latestInvestment
    ? safeNum(latestInvestment.total_pnl)
    : confirmedInvestment
      ? safeNum(confirmedInvestment.total_pnl)
      : investmentClasses.reduce((sum, cls) => sum + safeNum(cls.holding_pnl), 0);

  const categories = investmentClasses
    .map(c => ({ name: c.asset_class, value: safeNum(c.market_value) }))
    .sort((a, b) => b.value - a.value);
  if (investmentCashValue > 0.005) {
    categories.push({ name: '投资账户现金', value: investmentCashValue });
  }

  let html = '<div class="analysisList">';
  html += '<div style="font-weight:700;font-size:12px;color:var(--muted);margin-bottom:4px">投资资产权重</div>';
  for (const cat of categories) {
    const pct = investmentValue > 0 ? ((cat.value / investmentValue) * 100).toFixed(1) : '0.0';
    html += `<div class="analysisRow">
      <span class="analysisLabel">${cat.name}</span>
      <span class="analysisValue">${pct}%</span>
    </div>`;
  }

  html += '<div style="font-weight:700;font-size:12px;color:var(--muted);margin:12px 0 4px 0">分类投资收益</div>';
  for (const catName of INVESTMENT_CLASSES) {
    const cls = assetClasses.find(c => c.asset_class === catName);
    const hasConfirmed = Object.prototype.hasOwnProperty.call(confirmedByCategory, catName);
    const mv = cls ? safeNum(cls.market_value) : 0;
    const pnl = hasConfirmed ? safeNum(confirmedByCategory[catName]) : (cls ? safeNum(cls.holding_pnl) : 0);
    if (!cls && Math.abs(pnl) < 0.005) continue;
    const cost = mv - pnl;
    const pnlColor = pnl >= 0 ? 'var(--good)' : 'var(--bad)';
    const pnlSign = pnl >= 0 ? '+' : '-';
    const rate = cost > 0 ? Math.abs(pnl / cost * 100).toFixed(2) : '0.00';
    html += `<div class="analysisRow" style="flex-wrap:wrap">
      <div>
        <div style="font-size:13px;font-weight:600">${catName}</div>
        <div class="analysisSub">市值 ${fmtMoneyCompact(mv)}</div>
      </div>
      <div style="text-align:right">
        <div class="analysisValue" style="color:${pnlColor}">${fmtSignedMoneyCompact(pnl)}</div>
        <div class="analysisSub" style="color:${pnlColor}">${pnlSign}${rate}%</div>
      </div>
    </div>`;
  }

  const investmentCost = latestInvestment && safeNum(latestInvestment.cost) > 0
    ? safeNum(latestInvestment.cost)
    : safeNum(confirmedInvestment?.total_cost) > 0
      ? safeNum(confirmedInvestment.total_cost) + investmentCashValue
      : investmentValue - investmentPnl;
  const totalColor = investmentPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  const totalSign = investmentPnl >= 0 ? '+' : '-';
  const latestRate = latestInvestment?.time_weighted_return_rate ?? latestInvestment?.return_rate;
  const totalRate = latestInvestment
    ? Math.abs(safeNum(latestRate) * 100).toFixed(2)
    : investmentCost > 0 ? Math.abs(investmentPnl / investmentCost * 100).toFixed(2) : '0.00';
  const rateLabel = latestInvestment ? '时间加权收益率' : '收益率';
  html += `<div class="analysisRow" style="flex-wrap:wrap;padding:8px 0;border-top:2px solid var(--line)">
    <div>
      <div style="font-size:14px;font-weight:700">总投资收益</div>
      <div class="analysisSub">投资市值 ${fmtMoneyCompact(investmentValue)}</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:16px;font-weight:800;color:${totalColor}">${fmtSignedMoneyCompact(investmentPnl)}</div>
      <div class="analysisSub" style="color:${totalColor}">${rateLabel} ${totalSign}${totalRate}%</div>
    </div>
  </div>`;

  html += '</div>';
  el.innerHTML = html;
}

main().catch(err => {
  setActiveNav();
  document.getElementById('err').style.display = 'block';
  document.getElementById('err').textContent = '加载失败：' + String(err);
  console.error(err);
});
