import { createInvestmentChartController } from '../investment/charts/controller.js';
import { esc, fmtCny, fmtSignedCny, formatNumber, formatPercentRate } from '../investment/holdings/summary_legacy_model.js';
import { loadHoldingView } from '../investment/holdings/view_request.js';
import { loadChart } from '../../shared/load_chart.js';

export function createAssetOverview(root, {win, isActive, readAccounts}) {
  const find = id => root.querySelector('#' + id);
  const financing = find('assetFinancing');
  const cards = find('assetCards');
  let chart;
  let financingLoaded = false;
  let financingLoading = false;

  function renderNavCard(latest) {
    if (!isActive()) return;
    const money = value => value == null ? '—' : fmtCny(value);
    find('personalNetLabel').textContent = latest?.available_account_count != null ? '已知个人净资产' : '个人净资产';
    find('personalNet').textContent = money(latest?.market_value);
    find('personalGross').textContent = money(latest?.assets);
    find('personalDebt').textContent = money(latest?.liabilities);
    const loanPending = latest?.personal_loan_coverage?.status === 'principal_movements_only';
    find('personalCoverage').hidden = !latest || (!loanPending && latest.assets != null && latest.liabilities != null);
    find('personalCoverageNote').textContent = loanPending
      ? '个人借款余额待核，总负债尚未核齐。' : '部分账户资料待核验，暂无法汇总总资产和总负债。';
    const quality = latest?.estimated_account_count ? ` · ${latest.estimated_account_count} 个账户含估算` : '';
    find('personalAsOf').textContent = latest ? latest.date + quality : '暂无净资产记录';
  }

  async function load() {
    if (!find('asset-overview')) return;
    void readFinancing();
    void onCards();
    chart ??= createInvestmentChartController({root, host: win, onNavUpdate: renderNavCard, loadChartClass: () => loadChart(win),
      presenters: {esc, fmtCny, fmtSignedCny, formatNumber, formatPercentRate}});
    try {
      await chart.loadNavOnce();
      renderNavCard(chart.state.navRows.at(-1));
    } catch {
      if (isActive()) find('personalAsOf').textContent = '净资产暂未读到';
    }
  }

  function debtRow(container, title, value, date, href) {
    const doc = container.ownerDocument;
    const row = doc.createElement(href ? 'a' : 'div');
    row.className = 'assetDebtRow';
    if (href) row.href = href;
    const name = doc.createElement('span');
    name.textContent = title;
    const amount = doc.createElement('strong');
    amount.textContent = value;
    const source = doc.createElement('small');
    source.textContent = date || '';
    row.append(name, amount, source);
    container.append(row);
  }

  async function readFinancing() {
    if (!financing.open || financingLoaded || financingLoading) return;
    financingLoading = true;
    const container = financing.querySelector('div');
    container.textContent = '正在读取…';
    try {
      const view = await loadHoldingView();
      if (!isActive()) return;
      const items = view?.account_cards?.investment_overview?.financing_items;
      financingLoaded = Array.isArray(items);
      container.textContent = !items ? '融资明细待更新' : items.length ? '' : '暂无投资融资';
      for (const item of items || []) {
        debtRow(container, item.name || item.account_name, item.amount_cny == null ? '—' : fmtCny(item.amount_cny), item.as_of);
      }
    } catch {
      if (isActive()) container.textContent = '融资明细暂未读到，请重新展开';
    } finally {
      financingLoading = false;
    }
  }

  function renderCreditCards(groups) {
    if (!cards) return;
    const container = cards.querySelector('div');
    const accounts = groups.flatMap(group => group.rows).filter(row => row.active && row.account_type === 'liability');
    container.textContent = accounts.length ? '' : '暂无信用卡账户';
    for (const account of accounts) {
      const data = account.summary;
      const amount = data?.balance_amount;
      const money = amount == null ? '—' : new Intl.NumberFormat('zh-CN', {
        style: 'currency', currency: data.balance_currency || account.currency
      }).format(amount);
      const query = new URLSearchParams({account: account.account_code, flow: 'personal_loan'});
      debtRow(container, account.label, money, [data?.balance_as_of, '查看借还款流水 ›'].filter(Boolean).join(' · '),
        'ledger.html?' + query + '#recent-flow-section');
    }
  }
  async function onCards() {
    if (!cards.open) return;
    const output = cards.querySelector('div');
    if (!output.childElementCount) output.textContent = '正在读取…';
    await readAccounts();
    if (isActive() && !find('retryAccountSync').hidden) output.textContent = '账户明细暂未读到，请重新展开';
  }
  financing?.addEventListener('toggle', readFinancing);
  cards?.addEventListener('toggle', onCards);
  return {load, renderCreditCards, dispose() {
    chart?.dispose();
    financing?.removeEventListener('toggle', readFinancing);
    cards?.removeEventListener('toggle', onCards);
  }};
}
