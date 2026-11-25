// Presentation only: canvas cannot resolve CSS custom properties itself.
// Read the same tokens as HTML/SVG. No asset classification or financial math.
const css = typeof getComputedStyle === 'function' && typeof document !== 'undefined'
  ? getComputedStyle(document.documentElement) : null;
const token = (name, fallback) => css?.getPropertyValue(name).trim() || fallback;

export const SERIES_COLORS = Object.freeze([
  '#47b5ff', '#d0d0d4', '#aaaab0', '#85858b',
  '#e4e4e8', '#b8b8bd', '#98989e', '#747479',
].map((fallback, index) => token(`--pams-chart-series-${index + 1}`, fallback)));

export const CHART_THEME = Object.freeze({
  text: token('--pams-text-secondary', '#8e8e93'),
  background: token('--pams-bg-canvas', '#000000'),
  grid: token('--pams-chart-grid', 'rgba(255,255,255,.055)'),
  zeroLine: 'rgba(255,255,255,.22)',
  line: SERIES_COLORS[0],
  fill: 'rgba(71,181,255,.14)',
  fillTop: 'rgba(71,181,255,.26)',
  fillBottom: 'rgba(71,181,255,0)',
  event: SERIES_COLORS[1],
});

export function verticalAreaGradient(context, topColor = CHART_THEME.fillTop, bottomColor = CHART_THEME.fillBottom) {
  const chart = context?.chart;
  const area = chart?.chartArea;
  const ctx = chart?.ctx;
  if (!ctx || !area || !Number.isFinite(area.top) || !Number.isFinite(area.bottom) || area.bottom <= area.top) {
    return CHART_THEME.fill;
  }
  const gradient = ctx.createLinearGradient(0, area.top, 0, area.bottom);
  gradient.addColorStop(0, topColor);
  gradient.addColorStop(1, bottomColor);
  return gradient;
}

// Shape and dash differences keep series distinguishable without hue.
export function seriesAppearance(index) {
  return {
    borderDash: [[], [6, 3], [2, 3], [8, 3, 2, 3]][index % 4],
    pointStyle: ['circle', 'rect', 'triangle', 'crossRot'][index % 4],
  };
}
