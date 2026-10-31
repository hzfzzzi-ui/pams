// Existing display bindings and legacy calculations are preserved; API business semantics remain upstream.
import { INDUSTRY_EXCLUDED_ASSET_TYPES, THEME_FAMILIES, UNDERLYING_SORTS, QUALITY_LABELS, TYPE_LABELS } from './constants.js?v=20261031-analysis-modules-v1';

export function createAnalysisTemplates({ state, model }) {
  const { esc, n, returnAttributionLookupHoldingDaily, fmtMoney, fmtPct, fmtSignedMoney, calibratedReturnAttributionData, confidenceClass, confidenceLabel, qualityClass, rowValue, rowWeight, exposureRows, exposureName, portfolioTotalValue, displayName, sortUnderlyingRows, sourceBreakdownText, isClosedAttributionRow, dailyFxValueText, returnAttributionFxDisplay } = model;

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
  if (state.activeDimension === 'industry') {
    const quality = state.lookthroughData?.quality || {};
    const excludedSummary = renderIndustryExcludedSummary();
    return `
      <div class="lookthroughNarrative">
        <strong>主营业务行业暴露</strong>
        <span>行业只使用 8 个大桶；现金、债券、非上市股权不进入行业图。已穿透 ${fmtPct(quality.coverage_ratio)}，未穿透 ${fmtMoney(quality.uncovered_value_cny)}。</span>
        ${excludedSummary}
      </div>
    `;
  }
  if (state.activeDimension === 'theme') {
    return `
      <div class="lookthroughNarrative">
        <strong>主题暴露</strong>
        <span>主题可重叠，一只持仓可以同时属于多个主题，因此主题暴露合计可能超过 100%。</span>
      </div>
    `;
  }
  if (state.activeDimension === 'asset_type') {
    return `
      <div class="lookthroughNarrative">
        <strong>资产类型分布</strong>
        <span>资产类型独立于行业和主题；ETF / 基金、现金、债券 / 货币基金、非上市股权只在该维度或风险摘要中出现。</span>
      </div>
    `;
  }
  if (state.activeDimension === 'market') {
    return `<div class="lookthroughNarrative"><strong>市种 / 交易市场</strong><span>市种表示资产在哪个交易市场承载，不等同于行业或主题。</span></div>`;
  }
  if (state.activeDimension === 'region') {
    return `<div class="lookthroughNarrative"><strong>地区风险</strong><span>地区表示经济体 / 地理风险；上市地和地区可能不同。</span></div>`;
  }
  if (state.activeDimension === 'currency') {
    return `<div class="lookthroughNarrative"><strong>币种暴露</strong><span>币种只反映估值和汇率风险，不进入行业或主题。</span></div>`;
  }
  if (state.activeDimension === 'underlying_exposure') {
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
  const filtered = state.activeThemeFamily === '全部'
    ? rows
    : rows.filter(row => row.family === state.activeThemeFamily);
  const filters = THEME_FAMILIES.map(family => `
    <button type="button" class="${family === state.activeThemeFamily ? 'active' : ''}" data-theme-family="${esc(family)}">${esc(family)}</button>
  `).join('');
  return `
    ${renderDimensionIntro(rows)}
    <div class="themeFamilyFilters">${filters}</div>
    <div class="themeExposureRows">
      ${filtered.length ? renderBarRows(filtered, { limit: 24 }) : '<div class="lookthroughEmpty">该 family 暂无主题暴露</div>'}
    </div>
  `;
}

function renderUnderlyingExposure(rows) {
  const sorted = sortUnderlyingRows(rows).slice(0, 20);
  const sortButtons = UNDERLYING_SORTS.map(([key, label]) => `
    <button type="button" class="${key === state.underlyingSort ? 'active' : ''}" data-underlying-sort="${key}">${esc(label)}</button>
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

function renderLowConfidenceAssets(items = []) {
  if (!items.length) return '<div class="lookthroughEmpty compact">暂无低可信资产</div>';
  return items.slice(0, 12).map(item => {
    const coverage = Math.max(Math.min(n(item.coverage_ratio), 1), 0);
    const marketValue = n(item.market_value_cny);
    const coveredValue = n(item.covered_value_cny ?? marketValue * coverage);
    const uncoveredValue = n(item.uncovered_value_cny ?? Math.max(marketValue - coveredValue, 0));
    const uncoveredWeight = n(item.uncovered_weight ?? (state.lookthroughData?.total_market_value_cny ? uncoveredValue / n(state.lookthroughData.total_market_value_cny) : 0));
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

function returnAttributionIntro() {
  if (state.activeReturnAttributionDimension === 'theme') {
    return '<div class="lookthroughNarrative"><strong>主题收益归因</strong><span>主题按 theme_score 加权且可重叠，合计不要求等于组合总收益。</span></div>';
  }
  if (state.activeReturnAttributionDimension === 'industry') {
    return '<div class="lookthroughNarrative"><strong>行业收益归因</strong><span>行业只使用股票和基金穿透后的底层，现金、短债、非上市股权和商品不进入股票行业口径。</span></div>';
  }
  if (state.activeReturnAttributionDimension === 'currency') {
    return '<div class="lookthroughNarrative"><strong>币种收益归因</strong><span>币种按实际持有工具归因；价格收益和汇率收益字段单独保留。</span></div>';
  }
  if (state.activeReturnAttributionDimension === 'underlying') {
    return '<div class="lookthroughNarrative"><strong>底层收益归因</strong><span>基金收益按底层权重分摊，未覆盖部分进入未穿透残差。</span></div>';
  }
  return '';
}

function returnAttributionRowHtml(row, index, maxAbsPnl) {
  const pnl = n(row.pnl_cny ?? row.total_pnl_cny ?? row.value_cny);
  const daily = state.activeReturnAttributionDimension === 'holding'
    ? returnAttributionLookupHoldingDaily(row)
    : n(row.daily_pnl_cny);
  const dailyPrice = row.daily_price_pnl_cny;
  const priceAsOf = calibratedReturnAttributionData()?.as_of || '待核准';
  const fx = returnAttributionFxDisplay(row);
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
        <em title="价格收益归因日期：${esc(priceAsOf)}">今日 ${fmtSignedMoney(daily)} · 价格收益（折人民币） ${dailyFxValueText(dailyPrice)}${fx ? ` · ${esc(fx.label)} ${dailyFxValueText(fx.value)}（日期未独立核验）` : ''}</em>
      </div>
    </button>
  `;
}

function dailyFxAccountBridgeHtml(row) {
  const sources = [row, ...(Array.isArray(row?.source_details) ? row.source_details : [])];
  if (!sources.some(source => String(source?.account || source?.account_code || '').trim().toLowerCase() === 'ibkr')) return '';
  const bridge = calibratedReturnAttributionData()?.daily_fx_attribution;
  if (!bridge || bridge.read_only !== true || bridge.affects_nav !== false
    || bridge.scope !== 'positions_and_signed_cash') return '';
  const status = bridge.status;
  const label = status === 'confirmed' ? (bridge.status_label || '已确认')
    : status === 'partial' ? '部分，待核准' : '待核准';
  const ownershipLabel = bridge.ownership_basis === 'owner_adjusted' ? '本人归属'
    : bridge.ownership_basis === 'full_account' ? '全账户' : '归属待核准';
  const summary = bridge.summary || {};
  const metrics = [
    ['账户净汇兑（证券及现金）', bridge.net_fx_pnl_cny, 'net_fx_pnl_cny'],
    ['证券汇兑', summary.position_fx_pnl_cny, 'position_fx_pnl_cny'],
    ['现金汇兑（含融资负现金）', summary.cash_fx_pnl_cny, 'cash_fx_pnl_cny'],
    ['价格收益（折人民币）', summary.price_pnl_cny, 'price_pnl_cny'],
    ['现金收益', summary.income_pnl_cny, 'income_pnl_cny'],
    ['费用', summary.expense_pnl_cny, 'expense_pnl_cny'],
    ['应计及其他 NAV 变化', bridge.accrual_other_change_cny, 'accrual_other_change_cny'],
    ['官方账户总收益', bridge.official_total_pnl_cny, 'official_total_pnl_cny'],
    ['未解释现金', summary.unexplained_cash_cny, 'unexplained_cash_cny'],
    ['剩余差额（官方收益桥接）', summary.official_residual_cny, 'official_residual_cny'],
  ];
  return `
    <div class="sheetSection" data-daily-fx-account-bridge>
      <h4>账户净汇兑（证券及现金）</h4>
      <p>${esc(bridge.opening_as_of || '期初日期待核准')} → ${esc(bridge.as_of || '期末日期待核准')} · IBKR ${ownershipLabel} · ${esc(label)} · ${status === 'confirmed' ? '独立日终证据' : '日终证据待核准'} · 只读，不影响 NAV</p>
      <div class="sheetSourceList">
        ${metrics.map(([name, value, key]) => `<div class="sheetSourceRow" data-daily-fx-field="${key}"><div><strong>${esc(name)}</strong></div><div><b>${dailyFxValueText(value, status)}</b>${['official_residual_cny', 'unexplained_cash_cny'].includes(key) && value != null ? `<small>${esc(value)} CNY</small>` : ''}</div></div>`).join('')}
        ${(Array.isArray(bridge.currencies) ? bridge.currencies : []).slice(0, 32).map(item => {
          const rowStatus = status === 'confirmed' ? item.status : status;
          return `<div class="sheetSourceRow" data-daily-fx-currency="${esc(item.currency)}">
            <div>
              <strong>${esc(item.currency)}</strong>
              <small>期初证券 ${esc(item.opening_positions ?? '待核准')} · 现金 ${esc(item.opening_cash ?? '待核准')}</small>
              <small>期初汇率 ${esc(item.opening_fx?.as_of || '日期待核准')} · ${esc(item.opening_fx?.rate ?? '待核准')} · ${esc(item.opening_fx?.source_label || '来源待核准')}</small>
              <small>期末汇率 ${esc(item.closing_fx?.as_of || '日期待核准')} · ${esc(item.closing_fx?.rate ?? '待核准')} · ${esc(item.closing_fx?.source_label || '来源待核准')}</small>
            </div>
            <div>
              <b>证券 ${dailyFxValueText(item.position_fx_pnl_cny, rowStatus)}</b>
              <span>现金 ${dailyFxValueText(item.cash_fx_pnl_cny, rowStatus)}</span>
              <span>净额 ${dailyFxValueText(item.net_fx_pnl_cny, rowStatus)}</span>
            </div>
          </div>`;
        }).join('')}
      </div>
    </div>
  `;
}

function returnAttributionDetailHtml(row, options = {}) {
  const details = row.source_details || [];
  const useHoldingDaily = options.useHoldingDaily === true || state.activeReturnAttributionDimension === 'holding';
  const dailyPnl = useHoldingDaily ? returnAttributionLookupHoldingDaily(row) : n(row.daily_pnl_cny);
  const dailyPricePnl = row.daily_price_pnl_cny;
  const priceAsOf = calibratedReturnAttributionData()?.as_of || '待核准';
  const fx = returnAttributionFxDisplay(row);
  return `
    <div class="sheetGrid">
      <div class="sheetMetric"><span>持仓盈亏</span><strong>${fmtSignedMoney(row.pnl_cny ?? row.value_cny)}</strong></div>
      <div class="sheetMetric"><span>今日盈亏</span><strong>${fmtSignedMoney(dailyPnl)}</strong></div>
      <div class="sheetMetric"><span title="价格收益归因日期：${esc(priceAsOf)}">价格收益（折人民币）</span><strong>${dailyFxValueText(dailyPricePnl)}</strong></div>
      ${fx ? `<div class="sheetMetric" data-instrument-fx><span>${esc(fx.label)}<small>日期未独立核验 · 仅证券，不含现金</small></span><strong>${dailyFxValueText(fx.value)}</strong></div>` : ''}
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
              <span>今日 ${fmtSignedMoney(useHoldingDaily ? returnAttributionLookupHoldingDaily(source) : source.daily_pnl_cny)}</span>
            </div>
          </div>
        `).join('') : '<p>暂无来源明细</p>'}
      </div>
    </div>
    ${dailyFxAccountBridgeHtml(row)}
  `;
}

  return { renderIndustryExcludedMetric, renderIndustryExcludedSummary, renderDimensionIntro, renderBarRows, renderThemeExposure, renderUnderlyingExposure, qualityBucket, renderLowConfidenceAssets, sourceDetailRows, aiSubThemeHtml, rowDetailHtml, returnAttributionIntro, returnAttributionRowHtml, dailyFxAccountBridgeHtml, returnAttributionDetailHtml };
}
