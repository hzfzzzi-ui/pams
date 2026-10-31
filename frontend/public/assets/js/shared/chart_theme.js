// Presentation only: canvas cannot resolve CSS custom properties itself.
// Read the same tokens as HTML/SVG. No asset classification or financial math.
const css = typeof getComputedStyle === 'function' && typeof document !== 'undefined'
  ? getComputedStyle(document.documentElement) : null;
const token = (name, fallback) => css?.getPropertyValue(name).trim() || fallback;

export const SERIES_COLORS = Object.freeze([
  '#f5f5f7', '#d0d0d4', '#aaaab0', '#85858b',
  '#e4e4e8', '#b8b8bd', '#98989e', '#747479',
].map((fallback, index) => token(`--pams-chart-series-${index + 1}`, fallback)));

export const CHART_THEME = Object.freeze({
  text: token('--pams-text-secondary', '#a1a1a6'),
  background: token('--pams-bg-canvas', '#000000'),
  grid: token('--pams-chart-grid', 'rgba(255,255,255,.08)'),
  line: SERIES_COLORS[0],
  fill: 'rgba(245,245,247,.08)',
  event: SERIES_COLORS[1],
});

// Shape and dash differences keep series distinguishable without hue.
export function seriesAppearance(index) {
  return {
    borderDash: [[], [6, 3], [2, 3], [8, 3, 2, 3]][index % 4],
    pointStyle: ['circle', 'rect', 'triangle', 'crossRot'][index % 4],
  };
}
