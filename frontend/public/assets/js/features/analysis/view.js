// Existing display bindings and legacy calculations are preserved; API business semantics remain upstream.
import { DIMENSIONS, CHART_DIMENSIONS, RETURN_ATTRIBUTION_DIMENSIONS, RETURN_ATTRIBUTION_DEFAULT_LIMIT } from './constants.js?v=20261031-analysis-modules-v1';

export function createAnalysisView({ state, root, model, templates, charts }) {
  const { esc, n, fmtMoney, fmtPct, calibratedReturnAttributionData, splitQualityIssues, returnAttributionRows } = model;
  const { renderDimensionIntro, renderBarRows, renderThemeExposure, renderUnderlyingExposure, qualityBucket, renderLowConfidenceAssets, returnAttributionIntro, returnAttributionRowHtml } = templates;
  const { renderChartBlock, ReturnAttributionAssetHeatmap } = charts;
  const getElementById = id => root.getElementById ? root.getElementById(id) : root.querySelector('[id="' + id + '"]');

function renderAnalysisPanelSwitch() {
  root.querySelectorAll('[data-analysis-panel-switch]').forEach((button) => {
    const isActive = button.dataset.analysisPanelSwitch === state.activeAnalysisPanel;
    button.classList.toggle('active', isActive);
    button.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  root.querySelectorAll('[data-analysis-panel-page]').forEach((panel) => {
    panel.hidden = panel.dataset.analysisPanelPage !== state.activeAnalysisPanel;
  });
}

function renderTabs() {
  const el = getElementById('lookthroughTabs');
  if (!el) return;
  el.innerHTML = DIMENSIONS.map(([key, label]) => `
    <button type="button" class="${key === state.activeDimension ? 'active' : ''}" data-lookthrough-dim="${key}">
      ${esc(label)}
    </button>
  `).join('');
}

function renderExposureRows() {
  const el = getElementById('lookthroughList');
  if (!el) return;
  const rows = state.lookthroughData?.exposures?.[state.activeDimension] || [];
  if (!rows.length) {
    el.innerHTML = `${renderDimensionIntro(rows)}<div class="lookthroughEmpty">穿透数据待补充</div>`;
    return;
  }
  if (state.activeDimension === 'theme') {
    el.innerHTML = renderThemeExposure(rows);
    return;
  }
  if (state.activeDimension === 'underlying_exposure') {
    el.innerHTML = renderUnderlyingExposure(rows);
    return;
  }
  const intro = renderDimensionIntro(rows);
  el.innerHTML = `${intro}${CHART_DIMENSIONS.has(state.activeDimension) ? renderChartBlock(rows) : renderBarRows(rows)}`;
}

function renderQuality() {
  const el = getElementById('lookthroughQuality');
  const badge = getElementById('lookthroughCoverageBadge');
  if (!el || !state.lookthroughData) return;
  const q = state.lookthroughData.quality || {};
  const total = n(state.lookthroughData.total_market_value_cny);
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

function renderReturnAttributionTabs() {
  const el = getElementById('returnAttributionTabs');
  if (!el) return;
  el.innerHTML = RETURN_ATTRIBUTION_DIMENSIONS.map(([key, label]) => `
    <button type="button" class="${key === state.activeReturnAttributionDimension ? 'active' : ''}" data-return-attr-dim="${key}">
      ${esc(label)}
    </button>
  `).join('');
}

function renderReturnAttributionSummary() {
  const badge = getElementById('returnAttributionBadge');
  const el = getElementById('returnAttributionSummary');
  const data = calibratedReturnAttributionData();
  if (!el || !data) return;
  if (badge) {
    const sourceText = data.source === 'holdings_page' ? ' · 持仓页口径' : '';
    badge.textContent = data.as_of ? `更新 ${data.as_of}${sourceText}` : `已计算${sourceText}`;
    badge.className = 'miniBadge data';
  }
  el.innerHTML = `
    ${ReturnAttributionAssetHeatmap(data)}
  `;
}

function renderReturnAttributionList() {
  const el = getElementById('returnAttributionList');
  if (!el) return;
  if (!calibratedReturnAttributionData()) {
    el.innerHTML = '<div class="lookthroughEmpty">收益归因数据待加载</div>';
    return;
  }
  const rows = returnAttributionRows();
  const maxAbsPnl = Math.max(...rows.map(row => Math.abs(n(row.pnl_cny ?? row.value_cny))), 0);
  const visibleEntries = (state.returnAttributionShowAll ? rows : rows.slice(0, RETURN_ATTRIBUTION_DEFAULT_LIMIT))
    .map((row, index) => ({ row, index }));
  const hiddenCount = Math.max(rows.length - RETURN_ATTRIBUTION_DEFAULT_LIMIT, 0);
  const selection = calibratedReturnAttributionData()?.quality?.underlying_selection;
  let selectionNotice = '';
  if (state.activeReturnAttributionDimension === 'underlying' && selection?.truncated) {
    const returned = Math.max(0, n(selection.returned_count));
    const total = Math.max(returned, n(selection.total_count));
    const requiredOmitted = Math.max(0, n(selection.required_omitted_count));
    const incomplete = requiredOmitted > 0 ? `；其中 ${requiredOmitted} 项关联标的因上限未返回，同名证券可能不完整` : '';
    selectionNotice = `<div class="lookthroughRuleText" role="status">底层数据返回 ${esc(returned)} / ${esc(total)} 项${esc(incomplete)}。</div>`;
  }
  const moreButton = hiddenCount > 0 ? `
    <button type="button" class="returnAttributionMoreBtn" data-return-attr-more>
      ${state.returnAttributionShowAll ? '收起' : `更多 ${hiddenCount} 项`}
    </button>
  ` : '';
  el.innerHTML = `
    ${returnAttributionIntro()}
    ${selectionNotice}
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

function renderLookthrough() {
  renderAnalysisPanelSwitch();
  renderQuality();
  renderTabs();
  renderExposureRows();
}

function renderAssetMaster() {
  renderAnalysisPanelSwitch();
  const summaryEl = getElementById('assetMasterSummary');
  const listEl = getElementById('assetMasterList');
  const badge = getElementById('assetMasterBadge');
  const data = state.lookthroughData?.master_data;
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

  return { renderAnalysisPanelSwitch, renderTabs, renderExposureRows, renderQuality, renderReturnAttributionTabs, renderReturnAttributionSummary, renderReturnAttributionList, renderReturnAttribution, renderLookthrough, renderAssetMaster };
}
