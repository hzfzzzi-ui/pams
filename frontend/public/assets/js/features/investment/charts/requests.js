import { jget } from '../../../api.js?v=20261031-gray-v1';

export const RETURN_SECTION_SPECS = [
    { key: 'a_stock', label: 'A股个股', url: '/investment/section-return-series?section=a_stock&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'a_fund', label: '基金', url: '/investment/section-return-series?section=a_fund&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'us_stock', label: '美股', url: '/investment/section-return-series?section=us_stock&start=2026-01-01&interval=daily&baseCurrency=CNY' },
    { key: 'precious_metal', label: '贵金属', url: '/investment/section-return-series?section=precious_metal&start=2026-01-01&interval=daily&baseCurrency=CNY' },
  ];

// Keep the existing URLs, parameters, cache policy and per-series empty-response fallback.
// jget shares in-flight reads and does not yet support cancellation; controllers guard completion.
export function readInvestmentNav() {
  return jget('/nav/weekly?start=2026-01-01&baseCurrency=CNY&latest=true&interval=daily')
    .catch(() => ({ rows: [] }));
}

export function readInvestmentReturnSeries() {
  return Promise.all([
    jget('/investment/return-series?start=2026-01-01&interval=daily&baseCurrency=CNY').catch(() => ({ rows: [] })),
    ...RETURN_SECTION_SPECS.map(spec => jget(spec.url).catch(() => ({ rows: [] }))),
  ]).then(([resp, ...sectionResponses]) => ({ resp, sectionResponses }));
}
