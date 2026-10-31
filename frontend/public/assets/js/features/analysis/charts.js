// Existing display bindings and legacy calculations are preserved; API business semantics remain upstream.
export function createAnalysisCharts({ state, root, model }) {
  const { esc, returnAttributionLookupHoldingDaily, fmtMoney, fmtPct, fmtSignedMoney, fmtCompactSignedMoney, rowWeight, displayName, chartSegments, chartVisibleLimit, returnAttributionHeatmapRows, returnAttributionAssetPnl, returnAttributionAssetWeight, returnAttributionAssetLabel, returnAttributionAssetMeta } = model;
  const getElementById = id => root.getElementById ? root.getElementById(id) : root.querySelector('[id="' + id + '"]');

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

function returnAttributionCompactName(value, maxLength = 8) {
  const label = String(value || '').trim();
  if (!label) return '';
  if (/[\u3400-\u9fff]/.test(label)) {
    const compacted = label
      .replace(/ETF联接[ABC]?$/i, '')
      .replace(/发起式联接[ABC]?$/i, '')
      .replace(/灵活配置混合[ABC]?$/i, '')
      .replace(/配置混合[ABC]?$/i, '')
      .replace(/混合[ABC]?$/i, '')
      .trim();
    return (compacted || label).slice(0, maxLength);
  }
  const cleaned = label
    .replace(/\b(INC|CORP|CO|LTD|PLC|CMN|CLASS|TRUST|SERIES)\b/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
  const words = (cleaned || label).split(/\s+/).filter(Boolean);
  return words.slice(0, Math.max(1, Math.min(2, Math.floor(maxLength / 4)))).join(' ') || label.slice(0, maxLength);
}

function returnAttributionTileText(row, rect) {
  const area = rect.w * rect.h;
  const shortSide = Math.min(rect.w, rect.h);
  const label = returnAttributionAssetLabel(row);
  if (area < 120 || shortSide < 7) return { meta: '', name: returnAttributionCompactName(label, 5) };
  if (area < 420 || shortSide < 12) return { meta: '', name: returnAttributionCompactName(label, 7) };
  if (label.length > 10 && (rect.w < 30 || shortSide < 18)) {
    return { meta: '', name: returnAttributionCompactName(label, 8) };
  }
  const meta = returnAttributionAssetMeta(row);
  return {
    meta: meta.length > 14 && rect.w < 34 ? '' : meta,
    name: label,
  };
}

function returnAttributionClamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function returnAttributionTreemapWeight(row) {
  return Math.max(Math.pow(returnAttributionAssetWeight(row), 0.9), 1);
}

function returnAttributionTreemapSplit(items) {
  const total = items.reduce((sum, item) => sum + item.layoutWeight, 0);
  const target = total / 2;
  let bestIndex = 1;
  let bestDiff = Number.POSITIVE_INFINITY;
  let running = 0;
  for (let index = 0; index < items.length - 1; index += 1) {
    running += items[index].layoutWeight;
    const diff = Math.abs(target - running);
    if (diff < bestDiff) {
      bestDiff = diff;
      bestIndex = index + 1;
    }
  }
  return bestIndex;
}

function returnAttributionTreemapPartition(items, rect) {
  if (!items.length) return [];
  if (items.length === 1) return [{ ...items[0], rect }];
  const total = items.reduce((sum, item) => sum + item.layoutWeight, 0);
  const splitIndex = returnAttributionTreemapSplit(items);
  const first = items.slice(0, splitIndex);
  const second = items.slice(splitIndex);
  const firstWeight = first.reduce((sum, item) => sum + item.layoutWeight, 0);
  const ratio = total > 0 ? firstWeight / total : 0.5;
  if (rect.w >= rect.h) {
    const firstWidth = rect.w * ratio;
    return [
      ...returnAttributionTreemapPartition(first, { x: rect.x, y: rect.y, w: firstWidth, h: rect.h }),
      ...returnAttributionTreemapPartition(second, { x: rect.x + firstWidth, y: rect.y, w: rect.w - firstWidth, h: rect.h }),
    ];
  }
  const firstHeight = rect.h * ratio;
  return [
    ...returnAttributionTreemapPartition(first, { x: rect.x, y: rect.y, w: rect.w, h: firstHeight }),
    ...returnAttributionTreemapPartition(second, { x: rect.x, y: rect.y + firstHeight, w: rect.w, h: rect.h - firstHeight }),
  ];
}

function returnAttributionTreemapLayout(rows) {
  const items = rows
    .map((row, index) => ({ row, index, layoutWeight: returnAttributionTreemapWeight(row) }))
    .sort((a, b) => b.layoutWeight - a.layoutWeight || a.index - b.index);
  return returnAttributionTreemapPartition(items, { x: 0, y: 0, w: 100, h: 100 });
}

function returnAttributionTileClass(rect) {
  const area = rect.w * rect.h;
  const shortSide = Math.min(rect.w, rect.h);
  if (area < 42 || shortSide < 3.9) return 'is-micro';
  if (area < 92 || shortSide < 5.8) return 'is-xs';
  if (area < 180 || shortSide < 8) return 'is-sm';
  if (area < 360) return 'is-md';
  if (area < 820) return 'is-lg';
  return 'is-xl';
}

function returnAttributionTileStyle(rect) {
  const area = rect.w * rect.h;
  const shortSide = Math.min(rect.w, rect.h);
  const rawBase = Math.sqrt(area) * 0.72 + shortSide * 0.16;
  const base = returnAttributionClamp(Math.min(rawBase, rect.w * 0.5 + 2.5, rect.h * 0.58 + 2), 7, 18);
  const meta = returnAttributionClamp(Math.min(base * 0.56, rect.w * 0.3 + 1.5), 6.2, 10.5);
  const value = returnAttributionClamp(Math.min(base * 0.9, rect.w * 0.42 + 2), 7, 15.5);
  const pad = returnAttributionClamp(shortSide * 0.34, 1.5, 9);
  const lines = rect.h >= 15 && rect.w >= 10 ? 2 : 1;
  return [
    `--return-asset-x:${rect.x.toFixed(3)}%`,
    `--return-asset-y:${rect.y.toFixed(3)}%`,
    `--return-asset-w:${rect.w.toFixed(3)}%`,
    `--return-asset-h:${rect.h.toFixed(3)}%`,
    `--return-asset-name:${base.toFixed(2)}px`,
    `--return-asset-meta:${meta.toFixed(2)}px`,
    `--return-asset-value:${value.toFixed(2)}px`,
    `--return-asset-pad:${pad.toFixed(2)}px`,
    `--return-asset-lines:${lines}`,
  ].join(';');
}

function ReturnAttributionAssetHeatmap(data) {
  const rows = returnAttributionHeatmapRows();
  if (!rows.length) return '<div class="lookthroughEmpty">暂无标的盈亏数据</div>';
  const layout = returnAttributionTreemapLayout(rows);
  const gainCount = rows.filter(row => returnAttributionAssetPnl(row) > 0.01).length;
  const lossCount = rows.filter(row => returnAttributionAssetPnl(row) < -0.01).length;
  return `
    <div class="returnAssetHeatmapPanel">
      <div class="returnAssetHeatmapHead">
        <div>
          <span>标的盈亏热力图</span>
          <strong>${esc(rows.length)} 个标的 · 持仓盈亏 ${fmtSignedMoney(data.total_pnl_cny)}</strong>
        </div>
        <div class="returnAssetHeatmapLegend">
          <span class="gain">盈利 ${gainCount}</span>
          <span class="loss">亏损 ${lossCount}</span>
        </div>
      </div>
      <div class="returnAssetHeatmapCanvas" data-return-asset-heatmap>
        ${layout.map((item) => {
          const { row, rect, index } = item;
          const pnl = returnAttributionAssetPnl(row);
          const dailyPnl = returnAttributionLookupHoldingDaily(row);
          const text = returnAttributionTileText(row, rect);
          const title = `${returnAttributionAssetMeta(row) || row.symbol || ''} ${returnAttributionAssetLabel(row)} ${fmtSignedMoney(pnl)}`.trim();
          return `
            <button type="button"
              class="returnAssetTile ${pnl >= 0 ? 'gain' : 'loss'} ${returnAttributionTileClass(rect)}"
              style="${returnAttributionTileStyle(rect)}"
              title="${esc(title)}"
              data-return-asset-heat-row="${index}">
              <span>${esc(text.meta)}</span>
              <strong>${esc(text.name)}</strong>
              <em>${esc(fmtCompactSignedMoney(pnl))}</em>
              <small>今日 ${esc(fmtCompactSignedMoney(dailyPnl))}</small>
            </button>
          `;
        }).join('')}
      </div>
    </div>
  `;
}

function setActiveChartSegment(segmentIndex, sourceEl = null) {
  const root = getElementById('lookthroughList');
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

  function dispose() {
    // These charts are SVG/HTML, with no Chart.js instance or resize listener.
    root.querySelectorAll('[data-lookthrough-chart], .returnAssetHeatmapPanel').forEach(node => node.remove());
  }

  return { polarPoint, arcPath, donutSvg, renderChartBlock, returnAttributionCompactName, returnAttributionTileText, returnAttributionClamp, returnAttributionTreemapWeight, returnAttributionTreemapSplit, returnAttributionTreemapPartition, returnAttributionTreemapLayout, returnAttributionTileClass, returnAttributionTileStyle, ReturnAttributionAssetHeatmap, setActiveChartSegment, dispose };
}
