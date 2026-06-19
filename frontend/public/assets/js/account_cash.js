const CASH_ACCOUNT_TYPES = new Set(['funding', 'cash_management', 'payment', 'wallet']);
const LIABILITY_ACCOUNT_TYPES = new Set(['liability', 'credit_card']);
const EXCLUDED_ACCOUNT_CODES = new Set(['direct_investment', 'huawei_employer']);

function safeNum(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function fxRate(currency, fxRates = {}) {
  const ccy = String(currency || 'CNY').trim().toUpperCase();
  if (!ccy || ccy === 'CNY') return 1;
  const direct = Number(fxRates[ccy]);
  return Number.isFinite(direct) && direct > 0 ? direct : 1;
}

function primaryBalance(account) {
  return account?.primary_balance || account?.manual_balance || null;
}

function isCashActiveAccount(account) {
  if (!account || account.include_in_nav === false) return false;
  const code = String(account.account_code || '');
  if (EXCLUDED_ACCOUNT_CODES.has(code)) return false;
  const type = String(account.account_type || '').toLowerCase();
  return CASH_ACCOUNT_TYPES.has(type) || LIABILITY_ACCOUNT_TYPES.has(type);
}

function accountMoney(account, fxRates = {}) {
  const balance = primaryBalance(account);
  if (!balance || balance.amount == null || balance.amount === '') return null;
  const accountType = String(account.account_type || '').toLowerCase();
  const currency = String(balance.currency || account.base_currency || 'CNY').toUpperCase();
  let amount = safeNum(balance.amount);
  if (!Number.isFinite(Number(balance.amount))) return null;
  if (LIABILITY_ACCOUNT_TYPES.has(accountType)) amount = -Math.abs(amount);
  const amountCny = amount * fxRate(currency, fxRates);
  return {
    amount,
    amountCny,
    currency,
    source: balance.source || account.truth_source || 'account_overview',
    asOf: balance.as_of || account.as_of || null,
  };
}

export function accountCashBreakdown(accountOverview, fxRates = {}) {
  const rows = [];
  let cashAssets = 0;
  let cashLiabilities = 0;
  const accounts = Array.isArray(accountOverview?.accounts) ? accountOverview.accounts : [];

  for (const account of accounts) {
    if (!isCashActiveAccount(account)) continue;
    const money = accountMoney(account, fxRates);
    if (!money || Math.abs(money.amountCny) < 0.005) continue;
    const row = {
      accountCode: account.account_code || '',
      displayName: account.display_name || account.account_code || '',
      accountType: account.account_type || '',
      amount: money.amount,
      amountCny: money.amountCny,
      currency: money.currency,
      source: money.source,
      asOf: money.asOf,
    };
    rows.push(row);
    if (money.amountCny < 0) {
      cashLiabilities += Math.abs(money.amountCny);
    } else {
      cashAssets += money.amountCny;
    }
  }

  return {
    rows,
    cashAssets,
    cashLiabilities,
    netCash: cashAssets - cashLiabilities,
  };
}

export function makeAccountCashHoldingRows(accountOverview, fxRates = {}) {
  return accountCashBreakdown(accountOverview, fxRates).rows.map((row) => ({
    symbol: `CASH_${String(row.accountCode || 'ACCOUNT').toUpperCase()}`,
    name: row.displayName,
    account: row.accountCode,
    account_code: row.accountCode,
    major_category: '现金',
    minor_category: '现金/活期',
    type: '现金',
    currency: row.currency,
    market_value: row.amount,
    market_value_cny: row.amountCny,
    market_value_native: row.amount,
    cost: row.amount,
    cost_cny: row.amountCny,
    holding_pnl: 0,
    holding_pnl_cny: 0,
    daily_pnl: 0,
    daily_pnl_cny: 0,
    current_price: null,
    quantity: null,
    data_source: row.source,
    asset_liability: row.amountCny < 0 ? '负债' : '资产',
  }));
}
