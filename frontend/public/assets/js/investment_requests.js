import { jget } from './api.js?v=20261031-modules-v1';

let summaryRequest = null;

export function loadInvestmentSummaryFresh({ signal } = {}) {
  // A controller may cancel only its own read; legacy overlapping readers still share.
  if (signal) return jget('/summary2/latest?baseCurrency=CNY', { cache: false, signal });
  if (!summaryRequest) {
    // Share only overlapping reads; a later request must still fetch fresh data.
    summaryRequest = jget('/summary2/latest?baseCurrency=CNY', { cache: false })
      .finally(() => { summaryRequest = null; });
  }
  return summaryRequest;
}
