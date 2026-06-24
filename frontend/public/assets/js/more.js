import { jget } from './api.js?v=20260619-investment-hardening-v1';
import { renderNavGrowthExplanationPanel } from './nav_growth_explanation.js?v=20260624-nav-quality-v1';
import { setActiveNav } from './ui.js?v=20260622-data-state-a11y-v1';
import { setText } from './utils.js?v=20260607-shared-format-v1';

function todayText() {
  return new Date().toISOString().slice(0, 10);
}

let navGrowthLoaded = false;

async function loadNavGrowthExplanationOnce() {
  const panel = document.getElementById('moreNavGrowthExplanationPanel');
  if (!panel || navGrowthLoaded) return;
  panel.innerHTML = '<div class="muted">正在加载NAV增值解释...</div>';
  try {
    const payload = await jget('/api/portfolio/equity-bridge?start=2026-01-01&baseCurrency=CNY', { cache: false });
    renderNavGrowthExplanationPanel(payload, panel);
    navGrowthLoaded = true;
  } catch (error) {
    panel.innerHTML = '<div class="muted"></div>';
    panel.firstElementChild.textContent = `NAV增值解释加载失败：${error?.message || String(error)}`;
  }
}

function bindNavGrowthDetails() {
  const details = document.getElementById('moreNavGrowthDetails');
  if (!details) return;
  if (details.open) loadNavGrowthExplanationOnce();
  details.addEventListener('toggle', () => {
    if (details.open) loadNavGrowthExplanationOnce();
  });
}

function boot() {
  setActiveNav();
  setText('asof', todayText());
  bindNavGrowthDetails();
}

boot();
