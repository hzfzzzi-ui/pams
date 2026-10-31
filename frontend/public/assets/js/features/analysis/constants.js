import { SERIES_COLORS } from '../../shared/chart_theme.js?v=20261031-gray-v1';

const DIMENSIONS = [
  ['asset_type', '资产类型'],
  ['industry', '行业'],
  ['theme', '主题暴露'],
  ['market', '市种'],
  ['region', '地区'],
  ['currency', '币种'],
  ['underlying_exposure', '底层暴露'],
];

const DIMENSION_KEYS = new Set(DIMENSIONS.map(([key]) => key));
const CHART_DIMENSIONS = new Set(['asset_type', 'industry', 'market', 'region', 'currency']);
const INDUSTRY_EXCLUDED_ASSET_TYPES = new Set([
  '现金',
  '债券 / 货币基金',
  '非上市股权',
  '黄金 / 商品',
  '房地产 / 实物资产',
  '应收款 / 待到账',
]);
const THEME_FAMILIES = ['全部', '科技成长', '资源通胀', '制造升级', '中国资产', '医药健康'];
const UNDERLYING_SORTS = [
  ['value', '有效金额'],
  ['weight', '有效权重'],
  ['industry', '行业'],
  ['theme', '主题'],
  ['source', '来源'],
];

const RETURN_ATTRIBUTION_DIMENSIONS = [
  ['holding', '标的'],
  ['industry', '行业'],
  ['region', '地区'],
  ['theme', '主题'],
  ['underlying', '基金底层'],
];

const RETURN_ATTRIBUTION_KEYS = new Set(RETURN_ATTRIBUTION_DIMENSIONS.map(([key]) => key));
const ANALYSIS_PANEL_KEYS = new Set(['return_attribution', 'trade_attribution', 'asset_master']);

const QUALITY_LABELS = {
  FULL: '完整',
  PARTIAL: '部分',
  TOP10_ONLY: 'Top10',
  INDEX_PROXY: '指数代理',
  PRODUCT_TAG_ONLY: '产品标签',
  MISSING: '缺映射',
  STALE: '过期',
};

const TYPE_LABELS = {
  DIRECT: '直接',
  FULL_HOLDINGS: '完整',
  TOP10: 'Top10',
  INDEX_PROXY: '指数代理',
  PRODUCT_TAG: '产品标签',
  MANUAL: '手工',
  MISSING: '缺失',
  direct: '直接',
  rule: '规则',
  fund_lookthrough: '基金穿透',
};

const EXPOSURE_COLORS = SERIES_COLORS;

const RETURN_ATTRIBUTION_DEFAULT_LIMIT = 5;

export { DIMENSIONS, DIMENSION_KEYS, CHART_DIMENSIONS, INDUSTRY_EXCLUDED_ASSET_TYPES, THEME_FAMILIES, UNDERLYING_SORTS, RETURN_ATTRIBUTION_DIMENSIONS, RETURN_ATTRIBUTION_KEYS, ANALYSIS_PANEL_KEYS, QUALITY_LABELS, TYPE_LABELS, EXPOSURE_COLORS, RETURN_ATTRIBUTION_DEFAULT_LIMIT };
