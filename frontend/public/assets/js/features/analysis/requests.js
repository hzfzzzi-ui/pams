import { jget } from '../../api.js?v=20261031-gray-v1';
import { loadInvestmentSummaryFresh } from '../../investment_requests.js?v=20261031-gray-v1';

export function createAnalysisRequests({ get = jget, summary = loadInvestmentSummaryFresh } = {}) {
  return {
    lookthrough: () => get('/api/analysis/lookthrough?baseCurrency=CNY'),
    attribution: () => get('/api/analysis/return-attribution?baseCurrency=CNY'),
    holdingDaily: () => summary(),
  };
}
