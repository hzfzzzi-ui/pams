import { jget } from './api.js?v=20261031-gray-v1';
import { renderNavGrowthExplanationPanel } from './nav_growth_explanation.js?v=20261031-gray-v1';
import { setActiveNav } from './ui.js?v=20261031-gray-v1';

export function issueSummary(payload) {
  const summary = payload?.data_quality?.summary;
  if (!summary || !['ok', 'warning', 'error'].includes(summary.status) ||
      !Number.isInteger(summary.error_count) || !Number.isInteger(summary.warning_count) ||
      summary.error_count < 0 || summary.warning_count < 0) throw new Error('数据状态暂不可用');
  return summary.status === 'ok' ? null : `错误 ${summary.error_count} · 警告 ${summary.warning_count}`;
}

async function loadIssues() {
  const row = document.getElementById('moreIssues');
  const label = document.getElementById('moreIssueText');
  try {
    const text = issueSummary(await jget('/api/portfolio/data-quality?baseCurrency=CNY', { cache: false }));
    row.hidden = text === null;
    if (text !== null) label.textContent = text;
  } catch {
    row.hidden = false;
    row.querySelector('strong').textContent = '数据状态暂不可用';
    label.textContent = '';
  }
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
  document.getElementById('moreOrigin').textContent = window.location.origin;
  bindNavGrowthDetails();
  loadIssues();
}

if (typeof document !== 'undefined') boot();
