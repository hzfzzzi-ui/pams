// Existing display bindings and legacy calculations are preserved; API business semantics remain upstream.
import { DIMENSIONS, CHART_DIMENSIONS, RETURN_ATTRIBUTION_DIMENSIONS, RETURN_ATTRIBUTION_DEFAULT_LIMIT } from './constants.js';

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

function scopedRows(rows) {
  if (state.riskScope !== 'ex_esop' || !Array.isArray(rows) || !rows.length) return rows;
  if (state.activeDimension === 'asset_type') {
    const filtered = rows.filter(r => String(r?.name || '') !== '非上市股权');
    const sum = filtered.reduce((acc, r) => acc + Math.max(n(r?.value_cny), 0), 0);
    if (sum <= 0) return filtered;
    return filtered.map(r => ({ ...r, weight: Math.max(n(r?.value_cny), 0) / sum }));
  }
  if (state.activeDimension === 'underlying_exposure') {
    const filtered = rows.filter(r => String(r?.asset_type || '') !== '非上市股权' && !/esop|内部股份/i.test(String(r?.name || '')));
    const sum = filtered.reduce((acc, r) => acc + Math.max(n(r?.value_cny), 0), 0);
    if (sum <= 0) return filtered;
    return filtered.map(r => ({ ...r, weight: Math.max(n(r?.value_cny), 0) / sum }));
  }
  return rows;
}

function renderExposureRows() {
  const el = getElementById('lookthroughList');
  if (!el) return;
  const rows = scopedRows(state.lookthroughData?.exposures?.[state.activeDimension] || []);
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

function qualityDrawerHtml() {
  if (!state.lookthroughData) return '<div class="lookthroughEmpty">穿透数据加载中</div>';
  const q = state.lookthroughData.quality || {};
  const total = n(state.lookthroughData.total_market_value_cny);
  const segments = q.segments?.length ? q.segments : [
    { label: '已穿透/直接持仓', value_cny: q.full_coverage_value_cny, quality_status: 'FULL' },
    { label: '指数/Top10 代理', value_cny: q.top10_only_value_cny, quality_status: 'INDEX_PROXY' },
    { label: '产品标签估算', value_cny: q.product_tag_only_value_cny, quality_status: 'PRODUCT_TAG_ONLY' },
    { label: '未穿透/缺映射', value_cny: q.missing_value_cny, quality_status: 'MISSING' },
    { label: '过期穿透数据', value_cny: q.stale_value_cny, quality_status: 'STALE' },
  ];
  const issueGroups = splitQualityIssues(q.low_confidence_assets || []);
  return `
    <div class="lookthroughQualityDrawer">
      <div class="lookthroughQualityDrawerHead">
        <span><b>${fmtMoney(q.covered_value_cny)}</b> 已覆盖</span>
        <em><b>${fmtMoney(q.uncovered_value_cny)}</b> 未覆盖</em>
      </div>
      <div class="lookthroughQualitySummary">
        ${segments.map(seg => qualityBucket(seg.label, seg.value_cny, total, seg.quality_status)).join('')}
      </div>
      <div class="lookthroughRuleText">高：直接持仓/完整持仓；中：指数代理或高覆盖 Top10；低：产品标签、覆盖不足、缺失或过期。</div>
      <details class="lookthroughIssues" open>
        <summary>未穿透残差 ${issueGroups.residual.length}</summary>
        ${renderLowConfidenceAssets(issueGroups.residual)}
      </details>
      <details class="lookthroughIssues">
        <summary>已覆盖但需核准 ${issueGroups.review.length}</summary>
        ${renderLowConfidenceAssets(issueGroups.review)}
      </details>
    </div>
  `;
}

function renderQuality() {
  const el = getElementById('lookthroughQuality');
  const badge = getElementById('lookthroughCoverageBadge');
  if (!state.lookthroughData) return;
  const q = state.lookthroughData.quality || {};
  const coverage = n(q.coverage_ratio);
  if (badge) {
    badge.textContent = `覆盖 ${fmtPct(coverage)} ›`;
    badge.className = `miniBadge ${coverage >= 0.7 ? 'data' : 'warn'} lookthroughQualityBtn`;
  }
  if (el) el.innerHTML = '';
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

function fmtSignedPct(val) {
  if (val === null || val === undefined || !Number.isFinite(Number(val))) return '--';
  const num = Number(val);
  return `${num >= 0 ? '+' : ''}${num.toFixed(1)}%`;
}

function fmtSignedYuan(val) {
  if (val === null || val === undefined || !Number.isFinite(Number(val))) return '--';
  const num = Number(val);
  const abs = Math.abs(num);
  const sign = num >= 0 ? '+' : '-';
  if (abs >= 10000) return `${sign}¥${(abs / 10000).toFixed(2)}万`;
  return `${sign}¥${Math.round(abs).toLocaleString('zh-CN')}`;
}

const BENCH_LINE_META = {
  total: { color: '#38bdf8', width: 2.4, primary: true },
  ssec: { color: '#f97316', width: 1.7, primary: false },
  kospi: { color: '#a78bfa', width: 1.7, primary: false },
  sp500: { color: '#34d399', width: 1.7, primary: false },
};

function compactTileName(raw, maxLen = 7) {
  const s = String(raw || '').trim();
  if (!s) return '';
  if (/[\u3400-\u9fff]/.test(s)) {
    const clean = s
      .replace(/ETF联接[ABC]?$/i, '')
      .replace(/发起式联接[ABC]?$/i, '')
      .replace(/灵活配置混合[ABC]?$/i, '')
      .replace(/混合[ABC]?$/i, '')
      .trim();
    return (clean || s).slice(0, maxLen);
  }
  const clean = s
    .replace(/\b(INC|CORP|CO|LTD|PLC|CMN|CLASS|TRUST|SERIES|ELECTRONICS)\b/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
  return (clean || s).slice(0, maxLen + 2);
}

function treemapPartition(items, rect) {
  if (!items.length) return [];
  if (items.length === 1) return [{ ...items[0], rect }];
  const total = items.reduce((sum, it) => sum + it.weight, 0);
  const half = total / 2;
  let splitIdx = 1;
  let bestDiff = Infinity;
  let acc = 0;
  for (let i = 0; i < items.length - 1; i += 1) {
    acc += items[i].weight;
    const diff = Math.abs(half - acc);
    if (diff < bestDiff) {
      bestDiff = diff;
      splitIdx = i + 1;
    }
  }
  const first = items.slice(0, splitIdx);
  const second = items.slice(splitIdx);
  const firstW = first.reduce((sum, it) => sum + it.weight, 0);
  const ratio = total > 0 ? firstW / total : 0.5;
  if (rect.w >= rect.h) {
    const w1 = rect.w * ratio;
    return [
      ...treemapPartition(first, { x: rect.x, y: rect.y, w: w1, h: rect.h }),
      ...treemapPartition(second, { x: rect.x + w1, y: rect.y, w: rect.w - w1, h: rect.h }),
    ];
  }
  const h1 = rect.h * ratio;
  return [
    ...treemapPartition(first, { x: rect.x, y: rect.y, w: rect.w, h: h1 }),
    ...treemapPartition(second, { x: rect.x, y: rect.y + h1, w: rect.w, h: rect.h - h1 }),
  ];
}

function buildPnlTreemapHtml(items, portfolio) {
  const valid = (items || [])
    .filter((r) => r && Number.isFinite(Number(r.pnl_cny)) && Math.abs(Number(r.pnl_cny)) >= 0.01)
    .sort((a, b) => Math.abs(Number(b.pnl_cny)) - Math.abs(Number(a.pnl_cny)))
    .slice(0, 12);
  const gainCount = valid.filter((r) => Number(r.pnl_cny) > 0).length;
  const lossCount = valid.filter((r) => Number(r.pnl_cny) < 0).length;
  const headerPnl = portfolio?.total_pnl_cny ?? portfolio?.ex_esop_pnl_cny;
  const headerPct = portfolio?.total_return_pct ?? portfolio?.ex_esop_return_pct;
  const myTone = headerPct == null ? '' : Number(headerPct) >= 0 ? 'good' : 'bad';

  if (!valid.length) {
    return `
      <div class="perfBlockCard perfTreemapCard">
        <div class="perfBlockHead">
          <h3>盈亏分布图</h3>
          <span class="perfBlockSub">含已清仓</span>
        </div>
        <div class="lookthroughEmpty compact">暂无盈亏分布数据</div>
      </div>
    `;
  }

  const weighted = valid.map((row) => ({
    row,
    weight: Math.max(Math.pow(Math.abs(Number(row.pnl_cny)), 0.72), 1),
  }));
  const layout = treemapPartition(weighted, { x: 0, y: 0, w: 100, h: 100 });

  const tilesHtml = layout.map(({ row, rect }) => {
    const pnl = Number(row.pnl_cny);
    const pct = row.pnl_pct != null && Number.isFinite(Number(row.pnl_pct)) ? Number(row.pnl_pct) : null;
    const isGain = pnl >= 0;
    const intensity = Math.max(0.32, Math.min(0.88, 0.36 + Math.min(Math.abs(pct ?? 20), 60) / 60 * 0.52));
    const bg = isGain
      ? `rgba(22, 163, 74, ${intensity.toFixed(2)})`
      : `rgba(220, 38, 38, ${intensity.toFixed(2)})`;
    const area = rect.w * rect.h;
    const shortSide = Math.min(rect.w, rect.h);
    const sizeCls = area >= 520 && shortSide >= 20
      ? 'is-lg'
      : area >= 240 && shortSide >= 14
        ? 'is-md'
        : 'is-sm';
    const displayName = compactTileName(row.name || row.symbol, sizeCls === 'is-lg' ? 10 : sizeCls === 'is-md' ? 7 : 6);
    const title = `${row.name || row.symbol} (${row.symbol || ''}${row.is_closed ? ' · 已清仓' : ''}) ${fmtSignedYuan(pnl)} (${fmtSignedPct(pct)})`;
    return `
      <button type="button"
        class="perfTreemapTile ${isGain ? 'is-gain' : 'is-loss'} ${sizeCls}"
        style="left:${rect.x.toFixed(3)}%;top:${rect.y.toFixed(3)}%;width:${rect.w.toFixed(3)}%;height:${rect.h.toFixed(3)}%;background:${bg};"
        title="${esc(title)}"
        data-jump-trade-review="${esc(row.symbol || '')}">
        <span class="perfTreemapName">${esc(displayName)}${row.is_closed && sizeCls === 'is-lg' ? ' <i>清仓</i>' : ''}</span>
        <strong class="perfTreemapPnl">${esc(fmtSignedYuan(pnl))}</strong>
        ${sizeCls === 'is-lg' && pct != null ? `<em class="perfTreemapPct">${esc(fmtSignedPct(pct))}</em>` : ''}
      </button>
    `;
  }).join('');

  return `
    <div class="perfBlockCard perfTreemapCard">
      <div class="perfBlockHead">
        <div>
          <h3>盈亏分布图</h3>
          <span class="perfBlockSub">含已清仓 · 前${valid.length}大盈亏标的</span>
        </div>
        <div class="perfTreemapMeta">
          <strong class="${myTone}">${esc(fmtSignedYuan(headerPnl))} <em>${esc(fmtSignedPct(headerPct))}</em></strong>
          <div class="perfTreemapLegend">
            <span class="good">盈 ${gainCount}</span>
            <span class="bad">亏 ${lossCount}</span>
          </div>
        </div>
      </div>
      <div class="perfTreemapCanvas" role="group" aria-label="红绿方块盈亏图">
        ${tilesHtml}
      </div>
    </div>
  `;
}

function buildBenchmarkChartModel(comparison) {
  const portfolio = comparison?.portfolio || {};
  const benchmarks = comparison?.benchmarks || [];
  const rawSeries = [
    {
      key: 'total',
      name: portfolio.name || '我的总收益率',
      ytd: portfolio.ytd_return_pct,
      asOf: portfolio.as_of,
      points: Array.isArray(portfolio.series) ? portfolio.series : [],
    },
    ...benchmarks.map((b) => ({
      key: b.key,
      name: b.name,
      ytd: b.ytd_return_pct,
      asOf: b.as_of,
      points: Array.isArray(b.series) ? b.series : [],
    })),
  ].filter((s) => s.points.length >= 2);

  const dateSet = new Set();
  rawSeries.forEach((s) => s.points.forEach((pt) => { if (pt?.date) dateSet.add(String(pt.date)); }));
  const dates = [...dateSet].sort();
  const series = rawSeries.map((s) => {
    const map = new Map(s.points.map((pt) => [String(pt.date), Number(pt.return_pct)]));
    let lastVal = null;
    const values = dates.map((d) => {
      if (map.has(d)) lastVal = map.get(d);
      return lastVal;
    });
    const meta = BENCH_LINE_META[s.key] || { color: '#94a3b8', width: 1.6, primary: false };
    return { ...s, ...meta, values };
  });
  return { dates, series };
}

function renderBenchmarkLineChart(container, comparison) {
  if (!container) return;
  const { dates, series } = buildBenchmarkChartModel(comparison);
  if (!dates.length || !series.length) {
    container.innerHTML = '<div class="lookthroughEmpty compact">基准走势数据待加载</div>';
    return;
  }

  if (!state.hiddenBenchKeys) state.hiddenBenchKeys = new Set();
  const activeSeries = series.filter((s) => !state.hiddenBenchKeys.has(s.key));
  const drawn = activeSeries.length ? activeSeries : [series[0]];

  const allVals = [0];
  drawn.forEach((s) => s.values.forEach((v) => { if (v != null && Number.isFinite(v)) allVals.push(v); }));
  const rawMin = Math.min(...allVals);
  const rawMax = Math.max(...allVals);

  const W = 360;
  const H = 206;
  const padL = 42;
  const padR = 10;
  const padT = 10;
  const padB = 22;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const xAt = (idx) => padL + (dates.length <= 1 ? 0 : (idx / (dates.length - 1)) * plotW);

  // Use a clean 2-zone piecewise linear scale when an outlier index (>55%) would squash the main [-30%, +30%] band.
  const useTwoZone = rawMax > 55 && rawMin >= -35;
  let yTicks;
  let yAt;
  if (useTwoZone) {
    const lowBound = rawMin < -25 ? -30 : -25;
    const midBreak = 30;
    const highBound = Math.ceil(rawMax / 30) * 30;
    const upperSplit = 0.32;
    const midY = padT + plotH * upperSplit;
    const botY = padT + plotH;
    yAt = (val) => {
      if (val >= midBreak) {
        const t = Math.min(1, Math.max(0, (val - midBreak) / (highBound - midBreak || 1)));
        return midY - t * (midY - padT);
      }
      const t = Math.min(1, Math.max(0, (midBreak - val) / (midBreak - lowBound || 1)));
      return midY + t * (botY - midY);
    };
    const midHigh = Math.round((midBreak + highBound) / 2 / 5) * 5;
    yTicks = [lowBound, -15, 0, 15, midBreak, midHigh, highBound];
  } else {
    const roughSpan = Math.max(rawMax - rawMin, 15);
    const step = roughSpan <= 30 ? 10 : roughSpan <= 70 ? 15 : 25;
    const minVal = Math.floor(rawMin / step) * step;
    const maxVal = Math.ceil(rawMax / step) * step;
    yAt = (val) => padT + ((maxVal - val) / (maxVal - minVal || 1)) * plotH;
    yTicks = [];
    for (let v = minVal; v <= maxVal + 0.01; v += step) yTicks.push(Math.round(v));
  }

  const yGridHtml = yTicks.map((v) => {
    const y = yAt(v).toFixed(1);
    const isZero = Math.abs(v) < 0.05;
    return `
      <line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"
        stroke="${isZero ? 'rgba(148,163,184,0.36)' : 'rgba(148,163,184,0.11)'}"
        stroke-dasharray="${isZero ? '3 3' : ''}" stroke-width="1" />
      <text x="${padL - 5}" y="${(Number(y) + 3).toFixed(1)}" text-anchor="end"
        fill="rgba(148,163,184,0.75)" font-size="9">${v > 0 ? '+' : ''}${Math.round(v)}%</text>
    `;
  }).join('');

  const xLabelIndices = [0, Math.floor((dates.length - 1) * 0.33), Math.floor((dates.length - 1) * 0.66), dates.length - 1];
  const xTicksHtml = [...new Set(xLabelIndices)].map((idx) => {
    const x = xAt(idx).toFixed(1);
    const label = dates[idx] ? dates[idx].slice(5) : '';
    return `<text x="${x}" y="${H - 5}" text-anchor="${idx === 0 ? 'start' : idx === dates.length - 1 ? 'end' : 'middle'}" fill="rgba(148,163,184,0.72)" font-size="9">${esc(label)}</text>`;
  }).join('');

  // Render benchmarks first, primary portfolio line on top.
  const ordered = [...drawn].sort((a, b) => Number(a.primary) - Number(b.primary));
  const pathsHtml = ordered.map((s) => {
    const coords = [];
    s.values.forEach((v, idx) => {
      if (v != null && Number.isFinite(v)) coords.push([xAt(idx), yAt(v)]);
    });
    if (coords.length < 2) return '';
    const d = coords.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const last = coords[coords.length - 1];
    let area = '';
    if (s.primary) {
      const zeroY = Math.min(padT + plotH, Math.max(padT, yAt(0)));
      area = `<path d="${d} L${last[0].toFixed(1)},${zeroY.toFixed(1)} L${coords[0][0].toFixed(1)},${zeroY.toFixed(1)} Z" fill="rgba(56,189,248,0.09)" />`;
    }
    return `
      ${area}
      <path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width}" stroke-linecap="round" stroke-linejoin="round" />
      <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="${s.primary ? 3.2 : 2.4}" fill="${s.color}" />
    `;
  }).join('');

  const legendHtml = series.map((s) => {
    const hidden = state.hiddenBenchKeys.has(s.key);
    const tone = s.ytd == null ? '' : Number(s.ytd) >= 0 ? 'good' : 'bad';
    return `
      <button type="button" class="perfBenchPill ${hidden ? 'is-off' : ''} ${s.primary ? 'is-primary' : ''}" data-bench-toggle="${esc(s.key)}">
        <i style="background:${s.color}"></i>
        <span>${esc(s.name)}</span>
        <strong class="${tone}">${esc(fmtSignedPct(s.ytd))}</strong>
      </button>
    `;
  }).join('');

  const formatReadout = (idx) => {
    const d = dates[idx] || '';
    const items = drawn.map((s) => {
      const v = s.values[idx];
      return `<span><i style="background:${s.color}"></i>${esc(s.name.replace('我的总收益率', '我的'))} <b>${esc(fmtSignedPct(v))}</b></span>`;
    }).join('');
    return `<em>${esc(d)}</em>${items}`;
  };

  container.innerHTML = `
    <div class="perfBlockCard perfBenchChartCard">
      <div class="perfBlockHead">
        <div>
          <h3>基准对比</h3>
        </div>
      </div>
      <div class="perfBenchPills">${legendHtml}</div>
      <div class="perfBenchReadout" data-bench-readout>${formatReadout(dates.length - 1)}</div>
      <div class="perfBenchSvgWrap">
        <svg class="perfBenchSvg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="我的总收益率与主要指数对比折线图">
          ${yGridHtml}
          ${xTicksHtml}
          ${pathsHtml}
          <line class="perfBenchCrosshair" data-bench-crosshair x1="0" y1="${padT}" x2="0" y2="${padT + plotH}" hidden />
          <g data-bench-dots></g>
          <rect class="perfBenchHit" x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent" />
        </svg>
      </div>
    </div>
  `;

  container.querySelectorAll('[data-bench-toggle]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.benchToggle;
      if (!key) return;
      if (state.hiddenBenchKeys.has(key)) state.hiddenBenchKeys.delete(key);
      else if (series.length - state.hiddenBenchKeys.size > 1) state.hiddenBenchKeys.add(key);
      renderBenchmarkLineChart(container, comparison);
    });
  });

  const svg = container.querySelector('.perfBenchSvg');
  const hit = container.querySelector('.perfBenchHit');
  const crosshair = container.querySelector('[data-bench-crosshair]');
  const dotsGroup = container.querySelector('[data-bench-dots]');
  const readout = container.querySelector('[data-bench-readout]');

  const showIndex = (idx) => {
    const clamped = Math.max(0, Math.min(dates.length - 1, idx));
    const cx = xAt(clamped);
    if (crosshair) {
      crosshair.setAttribute('x1', cx.toFixed(1));
      crosshair.setAttribute('x2', cx.toFixed(1));
      crosshair.removeAttribute('hidden');
    }
    if (dotsGroup) {
      dotsGroup.innerHTML = drawn.map((s) => {
        const v = s.values[clamped];
        if (v == null || !Number.isFinite(v)) return '';
        return `<circle cx="${cx.toFixed(1)}" cy="${yAt(v).toFixed(1)}" r="${s.primary ? 3.8 : 3}" fill="${s.color}" stroke="#0b1120" stroke-width="1.2" />`;
      }).join('');
    }
    if (readout) readout.innerHTML = formatReadout(clamped);
  };

  const resetPointer = () => {
    if (crosshair) crosshair.setAttribute('hidden', '');
    if (dotsGroup) dotsGroup.innerHTML = '';
    if (readout) readout.innerHTML = formatReadout(dates.length - 1);
  };

  if (svg && hit) {
    const handlePoint = (clientX) => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const relX = ((clientX - rect.left) / rect.width) * W;
      const ratio = Math.max(0, Math.min(1, (relX - padL) / plotW));
      showIndex(Math.round(ratio * (dates.length - 1)));
    };
    hit.addEventListener('pointermove', (e) => handlePoint(e.clientX));
    hit.addEventListener('pointerdown', (e) => handlePoint(e.clientX));
    hit.addEventListener('pointerleave', resetPointer);
  }
}

function renderPerformanceSummary() {
  const el = getElementById('performanceBenchmarkPanel');
  if (!el) return;
  const data = state.performanceSummaryData;
  if (!data) {
    el.innerHTML = '<div class="muted">正在加载基准对比与盈亏图...</div>';
    return;
  }
  const p = data.portfolio || {};
  const heatmapItems = data.heatmap_items || [
    ...(data.leaderboard?.winners || []),
    ...(data.leaderboard?.losers || []),
  ];
  const comparison = data.benchmark_comparison || {
    portfolio: { key: 'total', name: '我的总收益率', ytd_return_pct: p.ex_esop_return_pct, series: [] },
    benchmarks: data.benchmarks || [],
  };

  el.innerHTML = `
    <div class="perfSplitStack">
      ${buildPnlTreemapHtml(heatmapItems, p)}
      <div data-perf-benchmark-chart></div>
    </div>
  `;
  renderBenchmarkLineChart(el.querySelector('[data-perf-benchmark-chart]'), comparison);
}

function renderReturnAttribution() {
  renderAnalysisPanelSwitch();
  renderPerformanceSummary();
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

  return { renderAnalysisPanelSwitch, renderTabs, renderExposureRows, renderQuality, qualityDrawerHtml, renderPerformanceSummary, renderReturnAttributionTabs, renderReturnAttributionSummary, renderReturnAttributionList, renderReturnAttribution, renderLookthrough, renderAssetMaster };
}
