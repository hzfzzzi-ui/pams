import { jget } from './api.js?v=20261031-gray-v1';

let summaryRequest = null;

export function loadInvestmentSummaryFresh() {
  if (!summaryRequest) {
    // Share only overlapping reads; a later request must still fetch fresh data.
    summaryRequest = jget('/summary2/latest?baseCurrency=CNY', { cache: false })
      .finally(() => { summaryRequest = null; });
  }
  return summaryRequest;
}
