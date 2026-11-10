import { readFallback } from '../../../shared/read_scope.js';

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
      jgetFresh('/holdings/latest?limit=500').catch(error => readFallback(error, null)),
      jgetFresh('/api/accounts/overview').catch(error => readFallback(error, null)),
      jgetFresh('/api/fx-rates/latest?base_currency=CNY'),
      jgetFresh('/api/ibkr/performance').catch(error => readFallback(error, null)),
      jgetFresh('/api/account-performance/tonghuashun').catch(error => readFallback(error, null)),
      jgetFresh('/api/investment-returns/reported').catch(error => readFallback(error, null)),
      loadInvestmentSummaryFresh().catch(error => readFallback(error, null)),
      jgetFresh('/api/portfolio/valuation-state?baseCurrency=CNY').catch(error => readFallback(error, null)),
  ]);
}

export function readListInputs({ get: jget, withClosedParam, onHoldings, onSupplementError }) {
  const holdings = jget(withClosedParam('/holdings/latest')).then(payload => {
    onHoldings?.(payload);
    return payload;
  });
  const fallback = error => {
    const value = readFallback(error, null);
    onSupplementError?.();
    return value;
  };
  return Promise.all([
    holdings,
    jget('/api/accounts/overview').catch(fallback),
    jget('/api/fx-rates/latest?base_currency=CNY').catch(fallback),
    jget('/api/ibkr/performance').catch(fallback),
  ]);
}
