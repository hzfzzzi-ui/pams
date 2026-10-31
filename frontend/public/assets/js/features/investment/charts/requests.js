import { readFallback } from '../../../shared/read_scope.js?v=20261031-modules-v1';
import { jget } from '../../../api.js?v=20261031-modules-v1';

export const RETURN_SECTION_SPECS = [
    { key: 'a_stock', label: 'A股个股', url: '/investment/section-return-series?section=a_stock&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'a_fund', label: '基金', url: '/investment/section-return-series?section=a_fund&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'us_stock', label: '美股', url: '/investment/section-return-series?section=us_stock&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'precious_metal', label: '贵金属', url: '/investment/section-return-series?section=precious_metal&start=2026-01-01&interval=daily&baseCurrency=CNY' },
  ];

// Keep the existing URLs, parameters, cache policy and per-series empty-response fallback.
// Cancellation remains a rejected read; ordinary endpoint failures retain the old fallback.
export function readInvestmentNav({signal} = {}) {
  return jget('/nav/weekly?start=2026-01-01&baseCurrency=CNY&latest=true&interval=daily', {signal})
    .catch(error => readFallback(error, { rows: [] }));
}

export function readInvestmentReturnSeries({signal} = {}) {
  return Promise.all([
    jget('/investment/return-series?start=2026-01-01&interval=daily&baseCurrency=CNY', {signal}).catch(error => readFallback(error, { rows: [] })),
    ...RETURN_SECTION_SPECS.map(spec => jget(spec.url, {signal}).catch(error => readFallback(error, { rows: [] }))),
  ]).then(([resp, ...sectionResponses]) => ({ resp, sectionResponses }));
}
