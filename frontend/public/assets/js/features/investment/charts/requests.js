import { readFallback } from '../../../shared/read_scope.js';
import { jget } from '../../../api.js';

export const RETURN_SECTION_SPECS = [
    { key: 'stocks', label: '股票', url: '/investment/section-return-series?section=stocks&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'a_fund', label: '基金', url: '/investment/section-return-series?section=a_fund&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'precious_metal', label: '贵金属', url: '/investment/section-return-series?section=precious_metal&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'wealth', label: '理财', url: '/investment/section-return-series?section=wealth&start=2026-01-01&interval=daily&baseCurrency=CNY', optional: true },
  ];

// Cancellation remains a rejected read; ordinary endpoint failures retain the old fallback.
export function readInvestmentNav({signal} = {}) {
  return jget('/nav/weekly?start=overview&baseCurrency=CNY&latest=true&interval=daily', {signal, cache: false})
    .catch(error => readFallback(error, { rows: [] }));
}

export function readInvestmentAnalysisView({signal} = {}) {
  return jget('/api/investment/analysis-view', {signal, cache: false})
    .catch(error => readFallback(error, null));
}

export function readInvestmentReturnSeries({signal, section} = {}) {
  return jget(section?.url || '/investment/return-series?start=2026-01-01&interval=daily&baseCurrency=CNY', {signal})
    .catch(error => readFallback(error, { rows: [] }));
}
