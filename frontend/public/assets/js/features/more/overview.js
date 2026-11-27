import {fmtCny} from '../investment/holdings/summary_legacy_model.js';
import {loadHoldingView} from '../investment/holdings/view_request.js';
import {createMonthlyOverview} from './monthly_overview.js';

export function createAssetOverview(root, {win, read, isActive, readAccounts}) {
  const find = id => root.querySelector('#' + id);
  const financing = find('assetFinancing');
  const cards = find('assetCards');
  const report = createMonthlyOverview(find('monthlyOverview'), {win, read, isActive});
  let financingLoaded = false;
  let financingLoading = false;

  async function load() {
    void readFinancing();
    void onCards();
    await report.load();
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
    if (!output.childElementCount && !output.dataset.loaded) output.textContent = '正在读取…';
    const groups = await readAccounts();
    if (!isActive()) return;
    if (Array.isArray(groups)) {
      renderCreditCards(groups);
      output.dataset.loaded = '1';
    } else if (!find('retryAccountSync').hidden) {
      output.textContent = '账户明细暂未读到，请重新展开';
    }
  }
  financing?.addEventListener('toggle', readFinancing);
  cards?.addEventListener('toggle', onCards);
  return {load, renderCreditCards, dispose() {
    report.dispose();
    financing?.removeEventListener('toggle', readFinancing);
    cards?.removeEventListener('toggle', onCards);
  }};
}
