// Read-only API boundaries. DTOs pass through without financial adaptation.
function panel(get, signal, loadPanels, name, path) {
  const legacy = () => get(path, {cache: false, signal});
  return loadPanels ? loadPanels().then(value => value ? value[name] : legacy()) : legacy();
}

export function loadRiskDisclosure(get, signal, loadPanels) {
  return panel(get, signal, loadPanels, 'risk', '/api/portfolio/risk-disclosure?baseCurrency=CNY');
}

export function loadPortfolioConfig(get, signal, loadPanels) {
  return panel(get, signal, loadPanels, 'config', '/api/portfolio/config?baseCurrency=CNY');
}
