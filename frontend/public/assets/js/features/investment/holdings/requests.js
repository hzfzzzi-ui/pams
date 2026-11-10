import { readFallback } from '../../../shared/read_scope.js';

// Summary and list reads share available view payloads and retain legacy fallbacks.
function selectPart(view, name, legacy) {
  return view ? view.then(data => data ? data[name] : legacy()) : legacy();
}

export function readSummaryInputs({ get, withClosedParam, loadInvestmentSummaryFresh, loadHoldingView, loadAccountView }) {
  const jgetFresh = path => get(path, { cache: false });
  const view = loadHoldingView?.();
  const account = loadAccountView?.();
  const part = (name, legacy) => selectPart(view, name, legacy);
  const holding = (name, path) => part(name, () => jgetFresh(path));
  return Promise.all([
      holding('a_stock', '/holdings/by-category/A股?minor_category=A股&limit=500&include_closed=true'),
      holding('funds', withClosedParam('/holdings/by-category/A股?minor_category=A股基金&limit=500')),
      holding('funds_all', '/holdings/by-category/A股?minor_category=A股基金&limit=500&include_closed=true'),
      holding('us_stock', withClosedParam('/holdings/by-category/美股?limit=500')),
      holding('precious_metal', withClosedParam('/holdings/by-category/贵金属?limit=500')),
      holding('esop', withClosedParam('/holdings/by-category/ESOP?limit=500')),
      holding('holdings', '/holdings/latest?limit=500').catch(error => readFallback(error, null)),
      selectPart(account, 'overview', () => jgetFresh('/api/accounts/overview')).catch(error => readFallback(error, null)),
      jgetFresh('/api/fx-rates/latest?base_currency=CNY'),
      jgetFresh('/api/ibkr/performance').catch(error => readFallback(error, null)),
      selectPart(account, 'tonghuashun', () => jgetFresh('/api/account-performance/tonghuashun')).catch(error => readFallback(error, null)),
      jgetFresh('/api/investment-returns/reported').catch(error => readFallback(error, null)),
      part('summary', loadInvestmentSummaryFresh).catch(error => readFallback(error, null)),
      jgetFresh('/api/portfolio/valuation-state?baseCurrency=CNY').catch(error => readFallback(error, null)),
  ]);
}

export function readListInputs({ get: jget, withClosedParam, onHoldings, onSupplementError, loadHoldingView, loadAccountView }) {
  const legacy = () => jget(withClosedParam('/holdings/latest'));
  const request = loadHoldingView ? loadHoldingView().then(view => view ? view.holdings : legacy()) : legacy();
  const holdings = request.then(payload => {
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
    selectPart(loadAccountView?.(), 'overview', () => jget('/api/accounts/overview')).catch(fallback),
    jget('/api/ibkr/performance').catch(error => error.status === 404 ? null : fallback(error)),
  ]);
}
