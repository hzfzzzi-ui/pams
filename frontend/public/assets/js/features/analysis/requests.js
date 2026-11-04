import { jget } from '../../api.js';
import { loadInvestmentSummaryFresh } from '../../investment_requests.js';

export function createAnalysisRequests({ get = jget, summary = loadInvestmentSummaryFresh } = {}) {
  return {
    lookthrough: ({signal} = {}) => get('/api/analysis/lookthrough?baseCurrency=CNY', {signal}),
    attribution: ({signal} = {}) => get('/api/analysis/return-attribution?baseCurrency=CNY', {signal}),
    holdingDaily: ({signal} = {}) => summary({signal}),
  };
}
