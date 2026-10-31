// Read-only API boundaries. DTOs pass through without financial adaptation.
export function loadRiskDisclosure(get, signal) {
  return get('/api/portfolio/risk-disclosure?baseCurrency=CNY', {cache: false, signal});
}

export function loadPortfolioConfig(get, signal) {
  return get('/api/portfolio/config?baseCurrency=CNY', {cache: false, signal});
}
