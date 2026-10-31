// Preserve the original fourteen summary and four list reads, URL parameters,
// optional fallbacks and fresh-summary sharing. No financial aggregation here.
export function readSummaryInputs({ get, withClosedParam, loadInvestmentSummaryFresh }) {
  const jgetFresh = path => get(path, { cache: false });
  return Promise.all([
      jgetFresh('/holdings/by-category/A股?minor_category=A股&limit=500&include_closed=true'),
      jgetFresh(withClosedParam('/holdings/by-category/A股?minor_category=A股基金&limit=500')),
      jgetFresh('/holdings/by-category/A股?minor_category=A股基金&limit=500&include_closed=true'),
      jgetFresh(withClosedParam('/holdings/by-category/美股?limit=500')),
      jgetFresh(withClosedParam('/holdings/by-category/贵金属?limit=500')),
      jgetFresh(withClosedParam('/holdings/by-category/ESOP?limit=500')),
      jgetFresh('/holdings/latest?limit=500').catch(() => null),
      jgetFresh('/api/accounts/overview').catch(() => null),
      jgetFresh('/api/fx-rates/latest?base_currency=CNY'),
      jgetFresh('/api/ibkr/performance').catch(() => null),
      jgetFresh('/api/account-performance/tonghuashun').catch(() => null),
      jgetFresh('/api/investment-returns/reported').catch(() => null),
      loadInvestmentSummaryFresh().catch(() => null),
      jgetFresh('/api/portfolio/valuation-state?baseCurrency=CNY').catch(() => null),
  ]);
}

export function readListInputs({ get: jget, withClosedParam }) {
  return Promise.all([
    jget(withClosedParam('/holdings/latest')),
    jget('/api/accounts/overview').catch(() => null),
    jget('/api/fx-rates/latest?base_currency=CNY').catch(() => null),
    jget('/api/ibkr/performance').catch(() => null),
  ]);
}
