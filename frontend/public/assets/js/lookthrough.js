import { jget } from './api.js?v=20260617-linkage-v1';
import { openBottomSheet } from './ui.js?v=20260614-trade-mode-attribution-v12';

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
  ['holding', '持仓'],
  ['industry', '行业'],
  ['theme', '主题'],
  ['region', '地区'],
  ['currency', '币种'],
  ['underlying', '底层'],
  ['asset_type', '资产类型'],
  ['market', '市种'],
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

const EXPOSURE_COLORS = [
  '#4c8dff',
  '#2ebd85',
  '#f0b90b',
  '#a371f7',
  '#f6465d',
  '#14b8a6',
  '#8b949e',
  '#ec4899',
];

let activeDimension = localStorage.getItem('pams.lookthrough.dimension') || 'asset_type';
if (activeDimension === 'underlying_asset') activeDimension = 'underlying_exposure';
if (!DIMENSION_KEYS.has(activeDimension)) activeDimension = 'asset_type';

let activeThemeFamily = '全部';
let underlyingSort = 'value';
let lookthroughData = null;
let activeReturnAttributionDimension = localStorage.getItem('pams.returnAttribution.dimension') || 'holding';
if (!RETURN_ATTRIBUTION_KEYS.has(activeReturnAttributionDimension)) activeReturnAttributionDimension = 'holding';
const RETURN_ATTRIBUTION_DEFAULT_LIMIT = 5;
let returnAttributionShowAll = false;
let returnAttributionData = null;
function analysisPanelFromHash(hash = window.location.hash) {
  const key = String(hash || '').replace(/^#/, '');
  if (key === 'asset-master-panel' || key === 'asset-master') return 'asset_master';
  if (key === 'return-attribution') return 'return_attribution';
  if (key === 'trade-attribution-analysis') return 'trade_attribution';
  if (key === 'return-trend') return 'return_attribution';
  return null;
}

let activeAnalysisPanel = analysisPanelFromHash() || localStorage.getItem('pams.analysis.panel') || 'return_attribution';
if (!ANALYSIS_PANEL_KEYS.has(activeAnalysisPanel)) activeAnalysisPanel = 'return_attribution';
let lookthroughLoadPromise = null;
let returnAttributionLoadPromise = null;

function renderAnalysisPanelSwitch() {
  document.querySelectorAll('[data-analysis-panel-switch]').forEach((button) => {
    const isActive = button.dataset.analysisPanelSwitch === activeAnalysisPanel;
    button.classList.toggle('active', isActive);
    button.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  document.querySelectorAll('[data-analysis-panel-page]').forEach((panel) => {
    panel.hidden = panel.dataset.analysisPanelPage !== activeAnalysisPanel;
  });
}

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function n(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function fmtMoney(value) {
  const amount = n(value);
  return `${amount < 0 ? '-' : ''}¥${Math.abs(amount).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtPct(value) {
  return `${(n(value) * 100).toFixed(1)}%`;
}

function fmtSignedMoney(value) {
  const amount = n(value);
  return `${amount < 0 ? '-' : '+'}¥${Math.abs(amount).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function calibratedReturnAttributionData() {
  return returnAttributionData;
}

function confidenceClass(confidence) {
  const key = String(confidence || '').toUpperCase();
  if (key === 'HIGH') return 'ok';
  if (key === 'MEDIUM') return 'warn';
  return 'pending';
}

function confidenceLabel(confidence) {
  const key = String(confidence || '').toUpperCase();
  return { HIGH: '高', MEDIUM: '中', LOW: '低' }[key] || '低';
}

function qualityClass(status) {
  const key = String(status || '').toUpperCase();
  if (key === 'FULL') return 'ok';
  if (key === 'INDEX_PROXY' || key === 'TOP10_ONLY') return 'data';
  if (key === 'PRODUCT_TAG_ONLY' || key === 'STALE') return 'warn';
  return 'pending';
}

function rowValue(row) {
  return n(row?.value_cny ?? row?.total_exposure_cny ?? row?.effectiveMarketValue);
}

function rowWeight(row) {
  return n(row?.weight ?? row?.total_weight ?? row?.effectiveWeightInPortfolio);
}

function exposureRows(dimension) {
  const rows = lookthroughData?.exposures?.[dimension];
  return Array.isArray(rows) ? rows : [];
}

function exposureName(row) {
  return String(row?.display_name ?? row?.name ?? '');
}

function portfolioTotalValue() {
  const explicitTotal = n(lookthroughData?.total_market_value_cny);
  if (explicitTotal > 0) return explicitTotal;
  return exposureRows('asset_type').reduce((sum, row) => sum + rowValue(row), 0);
}

function displayName(row) {
  if (!row) return '';
  if (row.display_name) return row.display_name;
  return row.name;
}

function renderTabs() {
  const el = document.getElementById('lookthroughTabs');
  if (!el) return;
  el.innerHTML = DIMENSIONS.map(([key, label]) => `
    <button type="button" class="${key === activeDimension ? 'active' : ''}" data-lookthrough-dim="${key}">
      ${esc(label)}
    </button>
  `).join('');
}

function chartSegments(rows, maxItems = 6) {
  const visible = rows.slice(0, maxItems).map((row, index) => ({
    row,
    index,
    label: displayName(row),
    weight: Math.max(rowWeight(row), 0),
    value: rowValue(row),
    color: EXPOSURE_COLORS[index % EXPOSURE_COLORS.length],
  }));
  const otherValue = rows.slice(maxItems).reduce((sum, row) => sum + rowValue(row), 0);
  const otherWeight = Math.max(1 - visible.reduce((sum, item) => sum + item.weight, 0), 0);
  if (otherValue > 0.005 || otherWeight > 0.005) {
    visible.push({
      row: null,
      index: -1,
      label: '其他',
      weight: otherWeight,
      value: otherValue,
      color: EXPOSURE_COLORS[EXPOSURE_COLORS.length - 1],
    });
  }
  return visible.filter(item => item.weight > 0 || item.value > 0);
}

function polarPoint(cx, cy, radius, angleDeg) {
  const angleRad = (angleDeg - 90) * Math.PI / 180;
  return {
    x: cx + radius * Math.cos(angleRad),
    y: cy + radius * Math.sin(angleRad),
  };
}

function arcPath(cx, cy, radius, startAngle, endAngle) {
  const start = polarPoint(cx, cy, radius, startAngle);
  const end = polarPoint(cx, cy, radius, endAngle);
  const largeArc = endAngle - startAngle <= 180 ? 0 : 1;
  return `M ${start.x.toFixed(3)} ${start.y.toFixed(3)} A ${radius} ${radius} 0 ${largeArc} 1 ${end.x.toFixed(3)} ${end.y.toFixed(3)}`;
}

function donutSvg(segments) {
  const radius = 44;
  let startAngle = 0;
  return segments.map((seg, segmentIndex) => {
    const sweep = Math.max(seg.weight * 360, 0.6);
    const gap = Math.min(1.2, sweep * 0.22);
    const pathStart = startAngle + gap / 2;
    const pathEnd = startAngle + Math.max(sweep - gap / 2, 0.5);
    startAngle += sweep;
    return `
      <path class="lookthroughDonutArc"
        data-lookthrough-segment="${segmentIndex}"
        data-lookthrough-label="${esc(seg.label)}"
        data-lookthrough-pct="${esc(fmtPct(seg.weight))}"
        data-lookthrough-value="${esc(fmtMoney(seg.value))}"
        d="${arcPath(60, 60, radius, pathStart, pathEnd)}"
        fill="none"
        stroke="${seg.color}"
        stroke-width="18"
        stroke-linecap="butt">
        <title>${esc(seg.label)} ${esc(fmtPct(seg.weight))} ${esc(fmtMoney(seg.value))}</title>
      </path>
    `;
  }).join('');
}

function chartVisibleLimit(rows) {
  if (activeDimension === 'market') return rows.length;
  if (activeDimension === 'industry') return 8;
  return 6;
}

function renderChartBlock(rows) {
  const maxItems = chartVisibleLimit(rows);
  const segments = chartSegments(rows, maxItems);
  const centerCount = Math.min(maxItems, rows.length);
  const centerWeight = rows.slice(0, centerCount).reduce((sum, row) => sum + rowWeight(row), 0);
  const centerLabel = rows.length <= maxItems ? '全部' : `前 ${centerCount}`;
  const listRows = segments.map((seg, segmentIndex) => {
    const rowAttrs = seg.index >= 0 ? `data-lookthrough-row="${seg.index}"` : '';
    const label = seg.index >= 0 ? displayName(seg.row) : seg.label;
    return `
      <button type="button" class="lookthroughChartRow"
        data-lookthrough-segment-list="${segmentIndex}"
        ${rowAttrs}
        data-lookthrough-label="${esc(label)}"
        data-lookthrough-pct="${esc(fmtPct(seg.weight))}"
        data-lookthrough-value="${esc(fmtMoney(seg.value))}">
        <i style="background:${seg.color}"></i>
        <strong title="${esc(label)}">${esc(label)}</strong>
        <span>${fmtPct(seg.weight)}</span>
        <em>${fmtMoney(seg.value)}</em>
      </button>
    `;
  }).join('');
  return `
    <div class="lookthroughChartBlock">
      <div class="lookthroughDonut" data-lookthrough-chart>
        <svg class="lookthroughDonutSvg" viewBox="0 0 120 120" role="img" aria-label="风险暴露分布">
          <circle class="lookthroughDonutBase" cx="60" cy="60" r="44" fill="none" stroke-width="18"></circle>
          ${donutSvg(segments)}
        </svg>
        <div class="lookthroughDonutCenter">
          <strong data-lookthrough-center-value>${fmtPct(centerWeight)}</strong>
          <span data-lookthrough-center-label>${esc(centerLabel)}</span>
          <em data-lookthrough-center-amount></em>
        </div>
      </div>
      <div class="lookthroughChartList">${listRows}</div>
    </div>
  `;
}

function renderIndustryExcludedMetric(label, value, total) {
  const weight = total > 0 ? n(value) / total : 0;
  return `
    <div class="lookthroughNarrativeMetric">
      <span>${esc(label)}</span>
      <strong>${fmtMoney(value)}</strong>
      <em>敞口占比 ${fmtPct(weight)}</em>
    </div>
  `;
}

function renderIndustryExcludedSummary() {
  const rows = exposureRows('asset_type');
  if (!rows.length) return '';
  const total = portfolioTotalValue();
  const cashValue = rows
    .filter(row => exposureName(row) === '现金')
    .reduce((sum, row) => sum + rowValue(row), 0);
  const excludedValue = rows
    .filter(row => INDUSTRY_EXCLUDED_ASSET_TYPES.has(exposureName(row)))
    .reduce((sum, row) => sum + rowValue(row), 0);

  return `
    <div class="lookthroughNarrativeMetrics" aria-label="行业图外资产统计">
      ${renderIndustryExcludedMetric('现金', cashValue, total)}
      ${renderIndustryExcludedMetric('行业图外合计', excludedValue, total)}
    </div>
  `;
}

function renderDimensionIntro(rows) {
  if (activeDimension === 'industry') {
    const quality = lookthroughData?.quality || {};
    const excludedSummary = renderIndustryExcludedSummary();
    return `
      <div class="lookthroughNarrative">
        <strong>主营业务行业暴露</strong>
        <span>行业只使用 8 个大桶；现金、债券、非上市股权不进入行业图。已穿透 ${fmtPct(quality.coverage_ratio)}，未穿透 ${fmtMoney(quality.uncovered_value_cny)}。</span>
        ${excludedSummary}
      </div>
    `;
  }
  if (activeDimension === 'theme') {
    return `
      <div class="lookthroughNarrative">
        <strong>主题暴露</strong>
        <span>主题可重叠，一只持仓可以同时属于多个主题，因此主题暴露合计可能超过 100%。</span>
      </div>
    `;
  }
  if (activeDimension === 'asset_type') {
    return `
      <div class="lookthroughNarrative">
        <strong>资产类型分布</strong>
        <span>资产类型独立于行业和主题；ETF / 基金、现金、债券 / 货币基金、非上市股权只在该维度或风险摘要中出现。</span>
      </div>
    `;
  }
  if (activeDimension === 'market') {
    return `<div class="lookthroughNarrative"><strong>市种 / 交易市场</strong><span>市种表示资产在哪个交易市场承载，不等同于行业或主题。</span></div>`;
  }
  if (activeDimension === 'region') {
    return `<div class="lookthroughNarrative"><strong>地区风险</strong><span>地区表示经济体 / 地理风险；上市地和地区可能不同。</span></div>`;
  }
  if (activeDimension === 'currency') {
    return `<div class="lookthroughNarrative"><strong>币种暴露</strong><span>币种只反映估值和汇率风险，不进入行业或主题。</span></div>`;
  }
  if (activeDimension === 'underlying_exposure') {
    return `<div class="lookthroughNarrative"><strong>底层穿透暴露</strong><span>直接持仓和基金穿透持仓统一按有效金额排序，可查看行业、主题、市场、地区和币种。</span></div>`;
  }
  return rows?.length ? '' : '';
}

function renderBarRows(rows, options = {}) {
  const maxWeight = Math.max(...rows.map(row => rowWeight(row)), 0.0001);
  return rows.slice(0, options.limit || 20).map((row, index) => {
    const width = Math.max(Math.min(rowWeight(row) / maxWeight * 100, 100), 1);
    const label = displayName(row);
    const family = row.family ? `<span>${esc(row.family)}</span>` : '';
    const score = row.score_weighted ? `<span>强度 ${n(row.score_weighted).toFixed(2)}</span>` : '';
    const aiBadge = row.name === 'AI / 算力' && row.sub_themes?.length ? '<span class="miniBadge data">二级链条</span>' : '';
    return `
      <button type="button" class="lookthroughRow themeExposureRow" data-lookthrough-row="${index}">
        <div class="lookthroughRowHead">
          <strong title="${esc(label)}">${esc(label)}</strong>
          <span>${fmtPct(rowWeight(row))}</span>
        </div>
        <div class="lookthroughBar"><i style="width:${width}%"></i></div>
        <div class="lookthroughMeta">
          <span>${fmtMoney(rowValue(row))}</span>
          ${family}
          ${score}
          ${aiBadge}
        </div>
      </button>
    `;
  }).join('');
}

function renderThemeExposure(rows) {
  const filtered = activeThemeFamily === '全部'
    ? rows
    : rows.filter(row => row.family === activeThemeFamily);
  const filters = THEME_FAMILIES.map(family => `
    <button type="button" class="${family === activeThemeFamily ? 'active' : ''}" data-theme-family="${esc(family)}">${esc(family)}</button>
  `).join('');
  return `
    ${renderDimensionIntro(rows)}
    <div class="themeFamilyFilters">${filters}</div>
    <div class="themeExposureRows">
      ${filtered.length ? renderBarRows(filtered, { limit: 24 }) : '<div class="lookthroughEmpty">该 family 暂无主题暴露</div>'}
    </div>
  `;
}

function sortUnderlyingRows(rows) {
  const sorted = [...rows];
  if (underlyingSort === 'industry') {
    sorted.sort((a, b) => String(a.industry_bucket || '').localeCompare(String(b.industry_bucket || ''), 'zh-CN') || rowValue(b) - rowValue(a));
  } else if (underlyingSort === 'theme') {
    sorted.sort((a, b) => String((a.theme_names || [])[0] || '').localeCompare(String((b.theme_names || [])[0] || ''), 'zh-CN') || rowValue(b) - rowValue(a));
  } else if (underlyingSort === 'source') {
    sorted.sort((a, b) => String(a.source || '').localeCompare(String(b.source || ''), 'zh-CN') || rowValue(b) - rowValue(a));
  } else if (underlyingSort === 'weight') {
    sorted.sort((a, b) => rowWeight(b) - rowWeight(a));
  } else {
    sorted.sort((a, b) => rowValue(b) - rowValue(a));
  }
  return sorted;
}

function renderUnderlyingExposure(rows) {
  const sorted = sortUnderlyingRows(rows).slice(0, 20);
  const sortButtons = UNDERLYING_SORTS.map(([key, label]) => `
    <button type="button" class="${key === underlyingSort ? 'active' : ''}" data-underlying-sort="${key}">${esc(label)}</button>
  `).join('');
  const body = sorted.map((row, index) => {
    const themes = (row.theme_names || []).slice(0, 3).join(' / ') || '无';
    return `
      <button type="button" class="underlyingExposureRow" data-underlying-row="${index}">
        <strong title="${esc(row.name)}">${esc(row.name)}</strong>
        <span>${esc(row.source || '直接持仓')}</span>
        <b>${fmtMoney(rowValue(row))}</b>
        <em>${fmtPct(rowWeight(row))}</em>
        <span>${esc(row.industry_bucket || '未穿透 / 未映射')}</span>
        <small title="${esc(themes)}">${esc(themes)}</small>
      </button>
    `;
  }).join('');
  return `
    ${renderDimensionIntro(rows)}
    <div class="underlyingSortBar">${sortButtons}</div>
    <div class="underlyingExposureTable">
      <div class="underlyingExposureHead">
        <span>底层持仓</span><span>来源</span><span>金额</span><span>权重</span><span>行业</span><span>主题</span>
      </div>
      ${body || '<div class="lookthroughEmpty">暂无底层暴露</div>'}
    </div>
  `;
}

function renderExposureRows() {
  const el = document.getElementById('lookthroughList');
  if (!el) return;
  const rows = lookthroughData?.exposures?.[activeDimension] || [];
  if (!rows.length) {
    el.innerHTML = `${renderDimensionIntro(rows)}<div class="lookthroughEmpty">穿透数据待补充</div>`;
    return;
  }
  if (activeDimension === 'theme') {
    el.innerHTML = renderThemeExposure(rows);
    return;
  }
  if (activeDimension === 'underlying_exposure') {
    el.innerHTML = renderUnderlyingExposure(rows);
    return;
  }
  const intro = renderDimensionIntro(rows);
  el.innerHTML = `${intro}${CHART_DIMENSIONS.has(activeDimension) ? renderChartBlock(rows) : renderBarRows(rows)}`;
}

function qualityBucket(label, value, total, status) {
  const weight = total > 0 ? n(value) / total : 0;
  return `
    <div class="lookthroughQualityBucket">
      <span>${esc(label)}</span>
      <strong>${fmtPct(weight)}</strong>
      <em>${fmtMoney(value)}</em>
      <i class="${qualityClass(status)}"></i>
    </div>
  `;
}

function splitQualityIssues(items = []) {
  const residual = [];
  const review = [];
  items.forEach(item => {
    const uncoveredValue = n(item.uncovered_value_cny);
    const status = String(item.quality_status || '').toUpperCase();
    const coverage = Math.max(Math.min(n(item.coverage_ratio), 1), 0);
    const hasResidual = uncoveredValue > 0.005 || status === 'MISSING' || status === 'STALE' || (status === 'PRODUCT_TAG_ONLY' && coverage < 1);
    if (hasResidual) residual.push(item);
    else review.push(item);
  });
  return { residual, review };
}

function renderLowConfidenceAssets(items = []) {
  if (!items.length) return '<div class="lookthroughEmpty compact">暂无低可信资产</div>';
  return items.slice(0, 12).map(item => {
    const coverage = Math.max(Math.min(n(item.coverage_ratio), 1), 0);
    const marketValue = n(item.market_value_cny);
    const coveredValue = n(item.covered_value_cny ?? marketValue * coverage);
    const uncoveredValue = n(item.uncovered_value_cny ?? Math.max(marketValue - coveredValue, 0));
    const uncoveredWeight = n(item.uncovered_weight ?? (lookthroughData?.total_market_value_cny ? uncoveredValue / n(lookthroughData.total_market_value_cny) : 0));
    const hasResidual = uncoveredValue > 0.005;
    return `
      <div class="lookthroughIssueRow">
        <div>
          <strong>${esc(item.asset_name)}</strong>
          <small>${esc(item.current_mapping || '待核准')} · ${esc(item.reason || '数据待核准')}</small>
          <em>${esc(item.suggested_action || '补充数据')}</em>
        </div>
        <div class="lookthroughIssueSide">
          <span class="${hasResidual ? 'warn' : 'ok'}">${hasResidual ? '未穿透' : '已覆盖'} ${fmtMoney(hasResidual ? uncoveredValue : coveredValue)}</span>
          <b>${hasResidual ? `覆盖 ${fmtPct(coverage)} · 拖累 ${fmtPct(uncoveredWeight)}` : `覆盖 ${fmtPct(coverage)} · 来源需核准`}</b>
          <small>${hasResidual ? `已覆盖 ${fmtMoney(coveredValue)} / 总值 ${fmtMoney(marketValue)}` : `未穿透 ${fmtMoney(0)} / 总值 ${fmtMoney(marketValue)}`}</small>
          <i class="miniBadge ${qualityClass(item.quality_status)}">${esc(QUALITY_LABELS[item.quality_status] || item.quality_status)}</i>
        </div>
      </div>
    `;
  }).join('');
}

function renderQuality() {
  const el = document.getElementById('lookthroughQuality');
  const badge = document.getElementById('lookthroughCoverageBadge');
  if (!el || !lookthroughData) return;
  const q = lookthroughData.quality || {};
  const total = n(lookthroughData.total_market_value_cny);
  const coverage = n(q.coverage_ratio);
  if (badge) {
    badge.textContent = `覆盖 ${fmtPct(coverage)}`;
    badge.className = `miniBadge ${coverage >= 0.7 ? 'data' : 'warn'}`;
  }
  const segments = q.segments?.length ? q.segments : [
    { label: '已穿透/直接持仓', value_cny: q.full_coverage_value_cny, quality_status: 'FULL' },
    { label: '指数/Top10 代理', value_cny: q.top10_only_value_cny, quality_status: 'INDEX_PROXY' },
    { label: '产品标签估算', value_cny: q.product_tag_only_value_cny, quality_status: 'PRODUCT_TAG_ONLY' },
    { label: '未穿透/缺映射', value_cny: q.missing_value_cny, quality_status: 'MISSING' },
    { label: '过期穿透数据', value_cny: q.stale_value_cny, quality_status: 'STALE' },
  ];
  const issueGroups = splitQualityIssues(q.low_confidence_assets || []);
  el.innerHTML = `
    <details class="lookthroughQualityDetails">
      <summary>
        <span>穿透质量</span>
        <b>${fmtMoney(q.covered_value_cny)} 已覆盖</b>
        <em>${fmtMoney(q.uncovered_value_cny)} 未覆盖</em>
      </summary>
      <div class="lookthroughQualitySummary">
        ${segments.map(seg => qualityBucket(seg.label, seg.value_cny, total, seg.quality_status)).join('')}
      </div>
      <div class="lookthroughRuleText">高：直接持仓/完整持仓；中：指数代理或高覆盖 Top10；低：产品标签、覆盖不足、缺失或过期。</div>
    </details>
    <details class="lookthroughIssues">
      <summary>未穿透残差 ${issueGroups.residual.length}</summary>
      ${renderLowConfidenceAssets(issueGroups.residual)}
    </details>
    <details class="lookthroughIssues">
      <summary>已覆盖但需核准 ${issueGroups.review.length}</summary>
      ${renderLowConfidenceAssets(issueGroups.review)}
    </details>
  `;
}

function sourceBreakdownText(sourceBreakdown = {}) {
  const entries = Object.entries(sourceBreakdown || {});
  if (!entries.length) return '暂无';
  return entries
    .sort((a, b) => n(b[1]) - n(a[1]))
    .map(([key, value]) => `${TYPE_LABELS[key] || key} ${fmtMoney(value)}`)
    .join(' / ');
}

function sourceDetailRows(row) {
  const details = row.source_details || [];
  if (!details.length) return '<p>暂无来源明细</p>';
  return details.slice(0, 12).map(source => {
    const underlyingTop = source.underlying_top || [];
    const underlyingNames = source.underlying_names || [];
    const topLine = underlyingTop.length
      ? underlyingTop.map(item => `${item.name} ${fmtPct(item.weight)}`).join(' / ')
      : underlyingNames.join(' / ');
    const singleUnderlying = source.underlying_name
      && source.underlying_name !== source.source_asset_name
      && String(source.lookthrough_type || '').toUpperCase() !== 'DIRECT';
    return `
      <div class="sheetSourceRow">
        <div>
          <strong>${esc(source.source_asset_name)}</strong>
          <small>${esc(source.account || '未标注账户')} · ${esc(source.source_symbol || '')}</small>
          ${topLine ? `<small>Top底层：${esc(topLine)}</small>` : ''}
          ${!topLine && singleUnderlying ? `<small>底层：${esc(source.underlying_name)}</small>` : ''}
          ${source.sw_l1 ? `<small>申万一级：${esc(source.sw_l1)} · ${esc(source.industry_bucket_source || '')}</small>` : ''}
          ${(source.theme_names || []).length ? `<em>${esc(source.theme_names.join(' / '))}</em>` : ''}
        </div>
        <div>
          <b>${fmtMoney(source.contribution_to_bucket_cny)}</b>
          <span>${esc(TYPE_LABELS[source.lookthrough_type] || source.lookthrough_type || '')}</span>
          <i class="miniBadge ${confidenceClass(source.confidence)}">${confidenceLabel(source.confidence)}</i>
        </div>
      </div>
    `;
  }).join('');
}

function aiSubThemeHtml(row) {
  const subThemes = row.sub_themes || [];
  if (!subThemes.length) return '';
  return `
    <div class="sheetSection">
      <h4>AI 算力链详情</h4>
      <p>${esc(row.dedupe_rule || '父主题去重，子主题可重叠。')}</p>
      <div class="aiSubThemeGrid">
        ${subThemes.map(sub => `
          <div class="aiSubThemeCard">
            <strong>${esc(sub.name)}</strong>
            <span>${fmtPct(rowWeight(sub))} · ${fmtMoney(rowValue(sub))}</span>
            <em>近5日：${sub.strength_5d ?? '待计算'} · 近20日：${sub.strength_20d ?? '待计算'} · ${esc(sub.status || '待计算')}</em>
            <small>${(sub.top_sources || sub.main_sources || []).slice(0, 3).map(item => esc(item.name)).join(' / ') || '暂无贡献持仓'}</small>
          </div>
        `).join('')}
      </div>
    </div>
  `;
}

function rowDetailHtml(row) {
  const totalValue = row.total_exposure_cny ?? row.value_cny ?? row.effectiveMarketValue;
  return `
    <div class="sheetGrid">
      <div class="sheetMetric"><span>总暴露</span><strong>${fmtMoney(totalValue)}</strong></div>
      <div class="sheetMetric"><span>敞口占比</span><strong>${fmtPct(row.total_weight ?? row.weight ?? row.effectiveWeightInPortfolio)}</strong></div>
      <div class="sheetMetric"><span>直接暴露</span><strong>${fmtMoney(row.direct_value_cny)}</strong></div>
      <div class="sheetMetric"><span>基金穿透</span><strong>${fmtMoney(row.indirect_value_cny)}</strong></div>
      <div class="sheetMetric"><span>行业</span><strong>${esc(row.industry_bucket || row.name || '-')}</strong></div>
      <div class="sheetMetric"><span>可信度</span><strong>${confidenceLabel(row.confidence)}</strong></div>
    </div>
    ${aiSubThemeHtml(row)}
    <div class="sheetSection">
      <h4>来源结构</h4>
      <p>${esc(sourceBreakdownText(row.source_breakdown))}</p>
      <div class="sheetSourceList">${sourceDetailRows(row)}</div>
    </div>
  `;
}

function renderReturnAttributionTabs() {
  const el = document.getElementById('returnAttributionTabs');
  if (!el) return;
  el.innerHTML = RETURN_ATTRIBUTION_DIMENSIONS.map(([key, label]) => `
    <button type="button" class="${key === activeReturnAttributionDimension ? 'active' : ''}" data-return-attr-dim="${key}">
      ${esc(label)}
    </button>
  `).join('');
}

function returnAttributionRows() {
  return calibratedReturnAttributionData()?.dimensions?.[activeReturnAttributionDimension] || [];
}

function returnAttributionRowsForDimension(dimension) {
  return calibratedReturnAttributionData()?.dimensions?.[dimension] || [];
}

function returnAttributionIntro() {
  if (activeReturnAttributionDimension === 'theme') {
    return '<div class="lookthroughNarrative"><strong>主题收益归因</strong><span>主题按 theme_score 加权且可重叠，合计不要求等于组合总收益。</span></div>';
  }
  if (activeReturnAttributionDimension === 'industry') {
    return '<div class="lookthroughNarrative"><strong>行业收益归因</strong><span>行业只使用股票和基金穿透后的底层，现金、短债、非上市股权和商品不进入股票行业口径。</span></div>';
  }
  if (activeReturnAttributionDimension === 'currency') {
    return '<div class="lookthroughNarrative"><strong>币种收益归因</strong><span>币种按实际持有工具归因；价格收益和汇率收益字段单独保留。</span></div>';
  }
  if (activeReturnAttributionDimension === 'underlying') {
    return '<div class="lookthroughNarrative"><strong>底层收益归因</strong><span>基金收益按底层权重分摊，未覆盖部分进入未穿透残差。</span></div>';
  }
  return '';
}

function isClosedAttributionRow(row) {
  return row?.source === '已清仓/零市值';
}

function renderReturnAttributionSummary() {
  const badge = document.getElementById('returnAttributionBadge');
  const el = document.getElementById('returnAttributionSummary');
  const data = calibratedReturnAttributionData();
  if (!el || !data) return;
  if (badge) {
    const sourceText = data.source === 'holdings_page' ? ' · 持仓页口径' : '';
    badge.textContent = data.as_of ? `更新 ${data.as_of}${sourceText}` : `已计算${sourceText}`;
    badge.className = 'miniBadge data';
  }
  const notes = (data.notes || []).slice(0, 2).map(note => `<span>${esc(note)}</span>`).join('');
  el.innerHTML = `
    <div class="returnAttributionCards">
      <div><span>持仓盈亏</span><strong class="${n(data.total_pnl_cny) >= 0 ? 'gain' : 'loss'}">${fmtSignedMoney(data.total_pnl_cny)}</strong></div>
      <div><span>今日盈亏</span><strong class="${n(data.daily_pnl_cny) >= 0 ? 'gain' : 'loss'}">${fmtSignedMoney(data.daily_pnl_cny)}</strong></div>
      <div><span>价格收益</span><strong>${fmtSignedMoney(data.daily_price_pnl_cny)}</strong></div>
      <div><span>汇率收益</span><strong>${fmtSignedMoney(data.daily_fx_pnl_cny)}</strong></div>
    </div>
    ${notes ? `<div class="returnAttributionNotes">${notes}</div>` : ''}
  `;
}

function returnAttributionRowHtml(row, index, maxAbsPnl) {
  const pnl = n(row.pnl_cny ?? row.total_pnl_cny ?? row.value_cny);
  const daily = n(row.daily_pnl_cny);
  const width = maxAbsPnl > 0 ? Math.max(Math.abs(pnl) / maxAbsPnl * 100, 2) : 0;
  const isClosed = isClosedAttributionRow(row);
  const sourceLine = (row.top_sources || row.source_details || [])
    .slice(0, 3)
    .map(item => item.name || item.source_asset_name)
    .filter(Boolean)
    .join(' / ');
  const meta = [
    isClosed ? '已清仓' : '',
    `敞口 ${fmtPct(row.exposure_weight ?? row.weight)}`,
    `贡献 ${fmtPct(row.pnl_contribution)}`,
    sourceLine,
  ].filter(Boolean).join(' · ');
  return `
    <button type="button" class="returnAttributionRow ${pnl >= 0 ? 'is-gain' : 'is-loss'} ${isClosed ? 'is-closed' : ''}" data-return-attr-row="${index}">
      <div class="returnAttributionRowHead">
        <strong>${esc(row.display_name || row.name || row.symbol || '-')}</strong>
        <span>${fmtSignedMoney(pnl)}</span>
      </div>
      <div class="returnAttributionPnlBar"><i style="width:${width}%"></i></div>
      <div class="returnAttributionRowMeta">
        <span>${esc(meta)}</span>
        <em>今日 ${fmtSignedMoney(daily)} · 价格 ${fmtSignedMoney(row.daily_price_pnl_cny)} · 汇率 ${fmtSignedMoney(row.daily_fx_pnl_cny)}</em>
      </div>
    </button>
  `;
}

function renderReturnAttributionList() {
  const el = document.getElementById('returnAttributionList');
  if (!el) return;
  if (!calibratedReturnAttributionData()) {
    el.innerHTML = '<div class="lookthroughEmpty">收益归因数据待加载</div>';
    return;
  }
  const rows = returnAttributionRows();
  const maxAbsPnl = Math.max(...rows.map(row => Math.abs(n(row.pnl_cny ?? row.value_cny))), 0);
  const visibleEntries = (returnAttributionShowAll ? rows : rows.slice(0, RETURN_ATTRIBUTION_DEFAULT_LIMIT))
    .map((row, index) => ({ row, index }));
  const hiddenCount = Math.max(rows.length - RETURN_ATTRIBUTION_DEFAULT_LIMIT, 0);
  const moreButton = hiddenCount > 0 ? `
    <button type="button" class="returnAttributionMoreBtn" data-return-attr-more>
      ${returnAttributionShowAll ? '收起' : `更多 ${hiddenCount} 项`}
    </button>
  ` : '';
  el.innerHTML = `
    ${returnAttributionIntro()}
    ${visibleEntries.length ? visibleEntries.map(({ row, index }) => returnAttributionRowHtml(row, index, maxAbsPnl)).join('') : '<div class="lookthroughEmpty">暂无归因数据</div>'}
    ${moreButton}
  `;
}

function renderReturnAttribution() {
  renderAnalysisPanelSwitch();
  renderReturnAttributionSummary();
  renderReturnAttributionTabs();
  renderReturnAttributionList();
}

function returnAttributionDetailHtml(row) {
  const details = row.source_details || [];
  return `
    <div class="sheetGrid">
      <div class="sheetMetric"><span>持仓盈亏</span><strong>${fmtSignedMoney(row.pnl_cny ?? row.value_cny)}</strong></div>
      <div class="sheetMetric"><span>今日盈亏</span><strong>${fmtSignedMoney(row.daily_pnl_cny)}</strong></div>
      <div class="sheetMetric"><span>价格收益</span><strong>${fmtSignedMoney(row.daily_price_pnl_cny)}</strong></div>
      <div class="sheetMetric"><span>汇率收益</span><strong>${fmtSignedMoney(row.daily_fx_pnl_cny)}</strong></div>
      <div class="sheetMetric"><span>敞口金额</span><strong>${fmtMoney(row.market_value_cny)}</strong></div>
      <div class="sheetMetric"><span>敞口占比</span><strong>${fmtPct(row.exposure_weight ?? row.weight)}</strong></div>
    </div>
    <div class="sheetSection">
      <h4>贡献来源</h4>
      <div class="sheetSourceList">
        ${details.length ? details.slice(0, 16).map(source => `
          <div class="sheetSourceRow">
            <div>
              <strong>${esc(source.name || source.source_asset_name || '-')}</strong>
              <small>${esc(source.account || '未标注账户')} · ${esc(source.source_symbol || '')}</small>
              ${source.underlying_name ? `<small>底层：${esc(source.underlying_name)} ${esc(source.underlying_symbol || '')}</small>` : ''}
            </div>
            <div>
              <b>${fmtSignedMoney(source.pnl_cny)}</b>
              <span>今日 ${fmtSignedMoney(source.daily_pnl_cny)}</span>
            </div>
          </div>
        `).join('') : '<p>暂无来源明细</p>'}
      </div>
    </div>
  `;
}

function setActiveChartSegment(segmentIndex, sourceEl = null) {
  const root = document.getElementById('lookthroughList');
  if (!root) return;
  const key = String(segmentIndex);
  const activeSource = sourceEl
    || [...root.querySelectorAll('[data-lookthrough-segment], [data-lookthrough-segment-list]')]
      .find(el => String(el.dataset.lookthroughSegment ?? el.dataset.lookthroughSegmentList) === key);
  root.querySelectorAll('[data-lookthrough-segment], [data-lookthrough-segment-list]').forEach((el) => {
    const current = el.dataset.lookthroughSegment ?? el.dataset.lookthroughSegmentList;
    el.classList.toggle('is-active', String(current) === key);
  });
  const value = root.querySelector('[data-lookthrough-center-value]');
  const label = root.querySelector('[data-lookthrough-center-label]');
  const amount = root.querySelector('[data-lookthrough-center-amount]');
  if (value && activeSource?.dataset.lookthroughPct) value.textContent = activeSource.dataset.lookthroughPct;
  if (label && activeSource?.dataset.lookthroughLabel) label.textContent = activeSource.dataset.lookthroughLabel;
  if (amount) amount.textContent = activeSource?.dataset.lookthroughValue || '';
}

function currentRowsForClick() {
  const rows = lookthroughData?.exposures?.[activeDimension] || [];
  if (activeDimension === 'theme' && activeThemeFamily !== '全部') {
    return rows.filter(row => row.family === activeThemeFamily);
  }
  if (activeDimension === 'underlying_exposure') {
    return sortUnderlyingRows(rows).slice(0, 20);
  }
  return rows;
}

function renderLookthrough() {
  renderAnalysisPanelSwitch();
  renderQuality();
  renderTabs();
  renderExposureRows();
}

function renderAssetMaster() {
  renderAnalysisPanelSwitch();
  const summaryEl = document.getElementById('assetMasterSummary');
  const listEl = document.getElementById('assetMasterList');
  const badge = document.getElementById('assetMasterBadge');
  const data = lookthroughData?.master_data;
  if (!summaryEl || !listEl) return;
  if (!data || data.status !== 'ok') {
    if (badge) badge.textContent = '待同步';
    summaryEl.innerHTML = '<div class="lookthroughEmpty">资产底表数据待加载</div>';
    listEl.innerHTML = '';
    return;
  }
  if (badge) {
    badge.textContent = `当前 ${data.current_assets || 0} / 底表 ${data.total_assets || 0}`;
    badge.className = 'miniBadge data';
  }
  const coverage = data.coverage || {};
  const cards = [
    ['当前资产', data.current_assets || 0, '本页持仓和基金底层去重后标的'],
    ['底表资产', data.total_assets || 0, '资产主表累计标的'],
    ['行业映射', fmtPct(coverage.industry), '一级/二级行业覆盖'],
    ['主题映射', fmtPct(coverage.theme), '父主题暴露覆盖'],
    ['基金底层', data.fund_underlying_rows || 0, `${data.funds_with_underlyings || 0} 只基金有底层行`],
  ];
  summaryEl.innerHTML = `
    <div class="assetMasterCards">
      ${cards.map(([label, value, hint]) => `
        <div>
          <span>${esc(label)}</span>
          <strong>${esc(value)}</strong>
          <em>${esc(hint)}</em>
        </div>
      `).join('')}
    </div>
    <div class="lookthroughNarrative">
      <strong>底表口径</strong>
      <span>资产身份、资产类型、行业、主题、地区、币种和基金底层分别落表；分析页先读底表，缺口再走规则兜底。</span>
    </div>
  `;
  const contractRows = (data.field_contract || []).map(row => `
    <div class="assetMasterContractRow">
      <strong>${esc(row.table)}</strong>
      <span>${esc(row.meaning)}</span>
    </div>
  `).join('');
  const lowRows = (data.low_confidence_assets || []).map(row => `
    <div class="assetMasterIssueRow">
      <div>
        <strong>${esc(row.name || row.asset_id)}</strong>
        <small>${esc(row.symbol || row.asset_id)} · ${esc(row.asset_type || '其他资产')}</small>
      </div>
      <div>
        <span>${esc(row.industry_bucket || '未穿透 / 未映射')}</span>
        <em>主题 ${n(row.theme_count)} · ${esc(row.confidence || 'medium')}</em>
      </div>
    </div>
  `).join('');
  const fundTopRows = (data.fund_underlying_top10 || []).map(fund => {
    const rows = (fund.underlyings || []).map(row => `
      <div class="assetMasterFundHolding">
        <strong>${esc(row.name || row.symbol || '未命名底层')}</strong>
        <span>${esc(row.symbol || '')}</span>
        <em>${fmtPct(row.weight_in_fund)} · ${fmtMoney(row.value_cny)}</em>
        <small>${esc(row.industry_bucket || '未穿透 / 未映射')}</small>
      </div>
    `).join('');
    return `
      <details class="assetMasterFund" open>
        <summary>
          <strong>${esc(fund.fund_name || fund.fund_symbol || '未命名基金')}</strong>
          <span>${esc(fund.lookthrough_type || 'LOOKTHROUGH')} · 覆盖 ${fmtPct(fund.coverage_ratio)} · ${fmtMoney(fund.fund_market_value_cny)}</span>
        </summary>
        <div class="assetMasterFundHoldings">
          ${rows || '<div class="lookthroughEmpty compact">暂无底层明细</div>'}
        </div>
      </details>
    `;
  }).join('');
  listEl.innerHTML = `
    <div class="assetMasterSection">
      <h4>字段契约</h4>
      ${contractRows}
    </div>
    <div class="assetMasterSection">
      <h4>基金底层 Top10</h4>
      ${fundTopRows || '<div class="lookthroughEmpty compact">暂无基金底层明细</div>'}
    </div>
    <div class="assetMasterSection">
      <h4>待核准资产</h4>
      ${lowRows || '<div class="lookthroughEmpty compact">当前组合暂无低置信资产</div>'}
    </div>
  `;
}

function bindEvents() {
  const syncChartSelection = (event) => {
    const target = event.target?.closest?.('[data-lookthrough-segment], [data-lookthrough-segment-list]');
    if (!target) return;
    const segmentIndex = target.dataset.lookthroughSegment ?? target.dataset.lookthroughSegmentList;
    if (segmentIndex !== undefined) setActiveChartSegment(segmentIndex, target);
  };

  document.addEventListener('pointerover', syncChartSelection);
  document.addEventListener('touchstart', syncChartSelection, { passive: true });

  document.addEventListener('click', (event) => {
    const panelSwitch = event.target?.closest?.('[data-analysis-panel-switch]');
    if (panelSwitch) {
      activeAnalysisPanel = panelSwitch.dataset.analysisPanelSwitch || 'return_attribution';
      if (!ANALYSIS_PANEL_KEYS.has(activeAnalysisPanel)) activeAnalysisPanel = 'return_attribution';
      localStorage.setItem('pams.analysis.panel', activeAnalysisPanel);
      renderAnalysisPanelSwitch();
      loadActiveAnalysisPanel();
      return;
    }

    const returnTab = event.target?.closest?.('[data-return-attr-dim]');
    if (returnTab) {
      activeReturnAttributionDimension = returnTab.dataset.returnAttrDim;
      returnAttributionShowAll = false;
      localStorage.setItem('pams.returnAttribution.dimension', activeReturnAttributionDimension);
      renderReturnAttribution();
      return;
    }

    const returnMore = event.target?.closest?.('[data-return-attr-more]');
    if (returnMore) {
      returnAttributionShowAll = !returnAttributionShowAll;
      renderReturnAttributionList();
      return;
    }

    const tab = event.target?.closest?.('[data-lookthrough-dim]');
    if (tab) {
      activeDimension = tab.dataset.lookthroughDim;
      localStorage.setItem('pams.lookthrough.dimension', activeDimension);
      renderLookthrough();
      return;
    }

    const family = event.target?.closest?.('[data-theme-family]');
    if (family) {
      activeThemeFamily = family.dataset.themeFamily || '全部';
      renderLookthrough();
      return;
    }

    const sort = event.target?.closest?.('[data-underlying-sort]');
    if (sort) {
      underlyingSort = sort.dataset.underlyingSort || 'value';
      renderLookthrough();
      return;
    }

    const chartTarget = event.target?.closest?.('[data-lookthrough-segment], [data-lookthrough-segment-list]');
    if (chartTarget) {
      const segmentIndex = chartTarget.dataset.lookthroughSegment ?? chartTarget.dataset.lookthroughSegmentList;
      if (segmentIndex !== undefined) setActiveChartSegment(segmentIndex, chartTarget);
    }

    const rowButton = event.target?.closest?.('[data-lookthrough-row], [data-underlying-row]');
    if (rowButton && lookthroughData) {
      const index = Number(rowButton.dataset.lookthroughRow ?? rowButton.dataset.underlyingRow);
      const row = currentRowsForClick()[index];
      if (row) openBottomSheet(`穿透：${displayName(row)}`, rowDetailHtml(row));
    }

    const returnRowButton = event.target?.closest?.('[data-return-attr-row]');
    if (returnRowButton && returnAttributionData) {
      const index = Number(returnRowButton.dataset.returnAttrRow);
      const row = returnAttributionRows()[index];
      if (row) openBottomSheet(`收益归因：${row.display_name || row.name}`, returnAttributionDetailHtml(row));
    }
  });
}

async function loadLookthrough() {
  const panel = document.getElementById('lookthrough-exposure');
  const attributionPanel = document.getElementById('return-attribution');
  if (!panel && !attributionPanel) return;
  renderAnalysisPanelSwitch();
  const lookthroughRequest = jget('/api/analysis/lookthrough?baseCurrency=CNY')
    .then((data) => {
      lookthroughData = data;
      renderLookthrough();
      renderAssetMaster();
    })
    .catch((error) => {
      const el = document.getElementById('lookthroughList');
      if (el) el.innerHTML = `<div class="lookthroughEmpty">穿透接口暂不可用：${esc(error?.message || error)}</div>`;
    });
  const attributionRequest = jget('/api/analysis/return-attribution?baseCurrency=CNY')
    .then((data) => {
      returnAttributionData = data;
      renderReturnAttribution();
    })
    .catch((error) => {
      const el = document.getElementById('returnAttributionList');
      if (el) el.innerHTML = `<div class="lookthroughEmpty">收益归因接口暂不可用：${esc(error?.message || error)}</div>`;
    });
  await Promise.allSettled([lookthroughRequest, attributionRequest]);
}

function loadLookthroughData() {
  const panel = document.getElementById('lookthrough-exposure');
  if (!panel) return Promise.resolve();
  if (lookthroughData) {
    renderLookthrough();
    renderAssetMaster();
    return Promise.resolve(lookthroughData);
  }
  if (lookthroughLoadPromise) return lookthroughLoadPromise;
  lookthroughLoadPromise = jget('/api/analysis/lookthrough?baseCurrency=CNY')
    .then((data) => {
      lookthroughData = data;
      renderLookthrough();
      renderAssetMaster();
      return data;
    })
    .catch((error) => {
      const el = document.getElementById('lookthroughList');
      if (el) el.innerHTML = `<div class="lookthroughEmpty">风险暴露接口暂不可用：${esc(error?.message || error)}</div>`;
      throw error;
    })
    .finally(() => {
      lookthroughLoadPromise = null;
    });
  return lookthroughLoadPromise;
}

function loadReturnAttributionData() {
  const panel = document.getElementById('return-attribution');
  if (!panel) return Promise.resolve();
  if (returnAttributionData) {
    renderReturnAttribution();
    return Promise.resolve(returnAttributionData);
  }
  if (returnAttributionLoadPromise) return returnAttributionLoadPromise;
  returnAttributionLoadPromise = jget('/api/analysis/return-attribution?baseCurrency=CNY')
    .then((data) => {
      returnAttributionData = data;
      renderReturnAttribution();
      return data;
    })
    .catch((error) => {
      const el = document.getElementById('returnAttributionList');
      if (el) el.innerHTML = `<div class="lookthroughEmpty">收益归因接口暂不可用：${esc(error?.message || error)}</div>`;
      throw error;
    })
    .finally(() => {
      returnAttributionLoadPromise = null;
    });
  return returnAttributionLoadPromise;
}

function loadActiveAnalysisPanel() {
  if (activeAnalysisPanel === 'return_attribution') {
    return loadReturnAttributionData().catch(() => {});
  }
  if (activeAnalysisPanel === 'trade_attribution') {
    return Promise.resolve();
  }
  return loadLookthroughData().catch(() => {});
}

function isAnalysisPanelVisible() {
  if (document.body.dataset.investTab === 'analysis') return true;
  const panel = document.querySelector('[data-invest-panel="analysis"]');
  return Boolean(panel && panel.hidden === false);
}

function isRiskPanelVisible() {
  if (document.body.dataset.investTab === 'risk') return true;
  const panel = document.querySelector('[data-invest-panel="risk"]');
  return Boolean(panel && panel.hidden === false);
}

function scheduleVisibleAnalysisLoad() {
  if (!document.getElementById('return-attribution')) return;
  const hashPanel = analysisPanelFromHash();
  if (hashPanel && hashPanel !== activeAnalysisPanel) {
    activeAnalysisPanel = hashPanel;
    localStorage.setItem('pams.analysis.panel', activeAnalysisPanel);
  }
  renderAnalysisPanelSwitch();
  if (isAnalysisPanelVisible()) loadActiveAnalysisPanel();
}

function scheduleRiskExposureLoad() {
  if (!document.getElementById('lookthrough-exposure')) return;
  if (isRiskPanelVisible()) loadLookthroughData().catch(() => {});
}

function scheduleVisiblePanelLoads() {
  scheduleVisibleAnalysisLoad();
  scheduleRiskExposureLoad();
}

function observeAnalysisVisibility() {
  scheduleVisiblePanelLoads();
  window.addEventListener('hashchange', scheduleVisiblePanelLoads);
  if (typeof MutationObserver !== 'function') return;
  new MutationObserver(scheduleVisiblePanelLoads).observe(document.body, {
    attributes: true,
    attributeFilter: ['data-invest-tab'],
  });
}

bindEvents();
observeAnalysisVisibility();
