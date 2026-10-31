import { SERIES_COLORS } from '../../../shared/chart_theme.js?v=20261031-modules-v1';
export const INVESTMENT_PHONE_QUERY = '(min-width: 0px)';

export const CATEGORY_SECTION_IDS = [
  'section-a-stock-equity',
  'section-a-fund',
  'section-us-stock',
  'section-precious-metal',
  'section-esop',
];

export const RETURN_METHODS = [
  {
    key: 'total-return',
    label: '总盈亏率',
    text: '总盈亏 / 投入成本。',
  },
  {
    key: 'cash-flow',
    label: '资金流调整',
    text: '分类线参考。',
  },
  {
    key: 'reported',
    label: '截图收益',
    text: '需核准。',
  },
  {
    key: 'fx',
    label: '多币种',
    text: '含汇率。',
  },
];

export const CATEGORY_RETURN_CHART_SPECS = [
  {
    targetId: 'section-a-stock-equity',
    chartId: 'chartAStockEquity',
    url: '/investment/section-return-series?section=a_stock&start=2026-01-01&interval=daily&baseCurrency=CNY',
    color: SERIES_COLORS[0],
    title: 'A股个股收益率',
  },
  {
    targetId: 'section-a-fund',
    chartId: 'chartAFund',
    url: '/investment/section-return-series?section=a_fund&start=2026-01-01&interval=daily&baseCurrency=CNY',
    color: SERIES_COLORS[1],
    title: '基金收益率',
  },
  {
    targetId: 'section-us-stock',
    chartId: 'chartUSStock',
    url: '/investment/section-return-series?section=us_stock&start=2026-01-01&interval=daily&baseCurrency=CNY',
    color: SERIES_COLORS[2],
    title: '美股收益率',
  },
  {
    targetId: 'section-precious-metal',
    chartId: 'chartPreciousMetal',
    url: '/investment/section-return-series?section=precious_metal&start=2026-01-01&interval=daily&baseCurrency=CNY',
    color: SERIES_COLORS[3],
    title: '贵金属收益率',
  },
];
