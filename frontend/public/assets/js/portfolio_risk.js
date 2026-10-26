import { jget } from './api.js?v=20260619-investment-hardening-v1';
import { dataStateHtml } from './ui.js?v=20261026-startup-order-v1';
import { afterInvestmentStartup } from './investment_startup.js?v=20261026-startup-order-v1';

let currentCategories = [];
let expandedRiskCategoryId = null;

function normalizeRiskSectionTitles() {
  const panel = document.getElementById('portfolio-risk-disclosure');
  const checklistHeading = panel?.querySelector?.('[data-risk-checklist-title]');
  if (checklistHeading) checklistHeading.textContent = '风险检查清单';
}

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function clamp(value, min = 0, max = 100) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return min;
  return Math.max(min, Math.min(max, parsed));
}

function toneLabel(tone) {
  return {
    risk: '高风险',
    warn: '偏高',
    info: '观察',
    ok: '正常',
  }[tone] || '观察';
}

function countsFromCategory(category) {
  const checklist = category?.checklist || {};
  const counts = checklist.counts || {};
  const fallback = { ok: 0, info: 0, warn: 0, risk: 0 };
  for (const item of category?.items || []) {
    const tone = item?.tone || 'info';
    fallback[tone] = (fallback[tone] || 0) + 1;
  }
  return {
    ok: Number(counts.ok ?? checklist.passed ?? fallback.ok ?? 0),
    info: Number(counts.info ?? checklist.watch ?? fallback.info ?? 0),
    warn: Number(counts.warn ?? checklist.warning ?? fallback.warn ?? 0),
    risk: Number(counts.risk ?? checklist.failed ?? fallback.risk ?? 0),
  };
}

function passFailCounts(category) {
  const counts = countsFromCategory(category);
  return {
    passed: counts.ok,
    watched: counts.info + counts.warn,
    failed: counts.risk,
  };
}

function itemStatusLabel(tone) {
  return {
    risk: '不满足',
    warn: '偏高',
    info: '观察',
    ok: '满足',
  }[tone] || '观察';
}

function itemState(tone) {
  if (tone === 'ok') return 'pass';
  if (tone === 'risk') return 'fail';
  return 'watch';
}

function cleanAssetName(value) {
  const text = String(value || '').trim();
  if (!text) return '--';
  const parts = text.split(/\s+-\s+/);
  const name = parts.length > 1 ? parts.slice(1).join(' - ') : text;
  return name.replace(/\s+CMN$/i, '').trim() || text;
}

function categoryStatusCounts(dashboard) {
  const counts = { ok: 0, info: 0, warn: 0, risk: 0 };
  for (const row of dashboard?.category_scores || []) {
    const tone = row?.tone || 'info';
    counts[tone in counts ? tone : 'info'] += 1;
  }
  return counts;
}

function dashboardSeverity(dashboard) {
  const score = clamp(dashboard?.score ?? 0);
  const maxScore = clamp(dashboard?.max_score ?? 100, 1, 999);
  const needleAngle = -120 + clamp(score ?? 0) * 2.4;
  return {
    scoreLabel: dashboard?.score_label || score.toFixed(0),
    maxScoreLabel: `/${maxScore.toFixed(0)}`,
    needleAngle: needleAngle.toFixed(1),
    counts: categoryStatusCounts(dashboard),
  };
}

function dashboardView(payload) {
  const dashboard = payload?.dashboard || {};
  const tone = dashboard.status_tone || 'info';
  const severity = dashboardSeverity(dashboard);
  const counts = severity.counts;

  return `
    <section class="portfolioRiskDashboard ${esc(tone)}">
      <div
        class="portfolioRiskGauge"
        style="--needle-angle:${esc(severity.needleAngle)}deg"
      >
        <div class="portfolioRiskNeedle" aria-hidden="true"></div>
        <div class="portfolioRiskGaugeHub" aria-hidden="true"></div>
        <strong>${esc(severity.scoreLabel)}</strong>
        <span>${esc(severity.maxScoreLabel)}</span>
      </div>
      <div class="portfolioRiskDashboardBody">
        <div class="portfolioRiskDashboardTop">
          <div>
            <strong>${esc(dashboard.status_label || toneLabel(tone))}</strong>
          </div>
        </div>
        <div class="portfolioRiskDashboardStats">
          <span class="risk"><b>${esc(counts.risk)}</b><em>高风险</em></span>
          <span class="warn"><b>${esc(counts.warn)}</b><em>偏高</em></span>
          <span class="info"><b>${esc(counts.info)}</b><em>观察</em></span>
          <span class="ok"><b>${esc(counts.ok)}</b><em>正常</em></span>
        </div>
      </div>
    </section>
  `;
}

function sourceChips(sources) {
  const rows = (sources || []).slice(0, 3);
  if (!rows.length) return '';
  return `
    <div class="portfolioRiskInlineSources">
      ${rows.map((source) => `
        <span>${esc(cleanAssetName(source.name))} ${esc(source.weight_label || '')}</span>
      `).join('')}
    </div>
  `;
}

function ruleFormula(item) {
  const rule = item.rule || {};
  return rule.formula || `${item.name || '该项'}暴露值 / 组合净资产`;
}

function pctValue(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return '';
  const pct = parsed * 100;
  return `${pct >= 10 ? pct.toFixed(0) : pct.toFixed(1)}%`;
}

function thresholdChipData(item) {
  const rawWarn = item?.thresholds?.warn;
  const rawRisk = item?.thresholds?.risk;
  const warn = rawWarn === null || rawWarn === undefined ? NaN : Number(rawWarn);
  const risk = rawRisk === null || rawRisk === undefined ? NaN : Number(rawRisk);
  const hasWarn = Number.isFinite(warn);
  const hasRisk = Number.isFinite(risk);
  if (hasWarn && hasRisk) {
    return [
      { tone: 'ok', label: '安全', value: `<${pctValue(warn)}` },
      { tone: 'warn', label: '观察', value: `${pctValue(warn)}-${pctValue(risk)}` },
      { tone: 'risk', label: '风险', value: `>=${pctValue(risk)}` },
    ];
  }
  if (hasRisk) return [{ tone: 'risk', label: '红线', value: `>=${pctValue(risk)}` }];
  return [{ tone: 'info', label: '阈值', value: '配置 / 人工' }];
}

function compactAdjustment(text, tone) {
  if (tone === 'ok') return '保持';
  if (tone === 'info') return '继续观察';
  if (tone === 'warn') return '接近上限';
  const value = String(text || '');
  if (/权益/.test(value)) return '降权益 / 补现金';
  if (/融资|杠杆/.test(value)) return '降杠杆';
  if (/现金/.test(value)) return '补现金缓冲';
  if (/集中|单一|分散/.test(value)) return '分散持仓';
  if (/估值|流动性/.test(value)) return '控估值 / 提流动性';
  if (/汇率|币种/.test(value)) return '降单币种';
  return '降低暴露';
}

function ruleBlock(item, category, isPassed) {
  const rule = item.rule || {};
  const tone = item.tone || 'info';
  const linked = rule.linked_config_rule || category?.name || '风险披露';
  const chips = thresholdChipData(item);
  const action = compactAdjustment(rule.adjustment_direction, tone);
  const actionLabel = tone === 'ok' ? '状态' : tone === 'risk' ? '动作' : '提示';
  return `
    <div class="portfolioRiskCompactRule">
      <div class="portfolioRiskRuleStrip">
        <span class="portfolioRiskRuleChip current ${esc(tone)}">
          <em>当前</em><b>${esc(item.weight_label || '--')}</b><i>${esc(toneLabel(tone))}</i>
        </span>
        ${chips.map((chip) => `
          <span class="portfolioRiskRuleChip ${esc(chip.tone)}">
            <em>${esc(chip.label)}</em><b>${esc(chip.value)}</b>
          </span>
        `).join('')}
      </div>
      <div class="portfolioRiskRuleMeta">
        <span><em>配置</em><b>${esc(linked)}</b></span>
        <span class="${esc(itemState(tone))}"><em>${esc(actionLabel)}</em><b>${esc(action)}</b></span>
      </div>
    </div>
  `;
}

function inlineChecklistItem(item, category) {
  const tone = item.tone || 'info';
  const isPassed = tone === 'ok';
  const state = itemState(tone);
  const formula = ruleFormula(item);
  return `
    <section class="portfolioRiskInlineItem ${state}">
      <div class="portfolioRiskInlineBadge">${esc(itemStatusLabel(tone))}</div>
      <div class="portfolioRiskInlineMain">
        <div class="portfolioRiskInlineTop">
          <div class="portfolioRiskInlineName">
            <strong>${esc(item.name || '--')}</strong>
            <small title="${esc(formula)}">${esc(formula)}</small>
          </div>
          <span>${esc(item.weight_label || '--')}</span>
        </div>
        ${ruleBlock(item, category, isPassed)}
        ${sourceChips(item.sources || [])}
      </div>
    </section>
  `;
}

function expandedChecklist(category) {
  const items = category.items || [];
  return `
    <div class="portfolioRiskInlinePanel">
      ${items.map((item) => inlineChecklistItem(item, category)).join('') || '<div class="lookthroughEmpty">暂无风险项</div>'}
    </div>
  `;
}

function checklistCard(category, index) {
  const checklist = category.checklist || {};
  const score = clamp(checklist.score ?? category.score ?? 0);
  const tone = checklist.tone || category.tone || 'info';
  const total = Number(checklist.total || (category.items || []).length || 0);
  const sequence = String(index + 1).padStart(2, '0');
  const isExpanded = expandedRiskCategoryId === category.id;
  const passFail = passFailCounts(category);

  return `
    <section
      class="portfolioRiskCategory portfolioRiskChecklistCard ${esc(tone)} ${isExpanded ? 'expanded' : ''}"
      style="--risk-level:${esc(score)}%;"
    >
      <button
        type="button"
        class="portfolioRiskChecklistSummary"
        data-risk-category="${esc(category.id)}"
        aria-expanded="${isExpanded ? 'true' : 'false'}"
      >
        <div class="portfolioRiskChecklistTop">
          <div class="portfolioRiskChecklistTitle">
            <span>${esc(sequence)}</span>
            <div>
              <strong>${esc(category.name || '--')}</strong>
              <small>${esc(category.description || '')}</small>
            </div>
          </div>
          <em>${esc(checklist.status_label || toneLabel(tone))}</em>
        </div>
        <div class="portfolioRiskChecklistMeter" aria-hidden="true"><i></i></div>
        <div class="portfolioRiskChecklistMeta">
          <strong>${esc(category.headline || '暂无显著暴露')}</strong>
          <span>
            <b class="pass">满足 ${esc(passFail.passed)}</b>
            <b class="watch">观察 ${esc(passFail.watched)}</b>
            <b class="fail">不满足 ${esc(passFail.failed)}</b>
            <i>${esc(total)} 项</i>
          </span>
        </div>
        <div class="portfolioRiskExpandCue">${isExpanded ? '收起' : '展开'}</div>
      </button>
      ${isExpanded ? expandedChecklist(category) : ''}
    </section>
  `;
}

function renderRiskCategories() {
  const categoriesEl = document.getElementById('portfolioRiskCategories');
  if (!categoriesEl) return;
  if (!currentCategories.length) {
    categoriesEl.innerHTML = dataStateHtml({
      tone: 'empty',
      title: '暂无风险检查项',
      detail: '等待风险披露接口返回七类 checklist。',
      compact: true,
    });
    return;
  }
  categoriesEl.innerHTML = currentCategories.map(checklistCard).join('')
    || '<div class="lookthroughEmpty">风险披露数据为空</div>';
}

function renderRiskDisclosure(payload) {
  currentCategories = payload?.categories || [];
  expandedRiskCategoryId = null;
  const asOfEl = document.getElementById('portfolioRiskAsOf');
  const dashboardEl = document.getElementById('portfolioRiskDashboard');

  if (asOfEl) asOfEl.textContent = payload?.as_of || '--';
  if (dashboardEl) dashboardEl.innerHTML = dashboardView(payload);
  renderRiskCategories();
}

function bindRiskDisclosureEvents() {
  document.addEventListener('click', (event) => {
    const button = event.target?.closest?.('[data-risk-category]');
    if (!button) return;
    const categoryId = button.dataset.riskCategory;
    if (!currentCategories.some((item) => item.id === categoryId)) return;
    expandedRiskCategoryId = expandedRiskCategoryId === categoryId ? null : categoryId;
    renderRiskCategories();
    if (expandedRiskCategoryId) {
      document
        .querySelector(`[data-risk-category="${CSS.escape(expandedRiskCategoryId)}"]`)
        ?.closest('.portfolioRiskChecklistCard')
        ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  });
}

async function loadPortfolioRiskDisclosure() {
  if (!document.getElementById('portfolio-risk-disclosure')) return;
  try {
    const payload = await jget('/api/portfolio/risk-disclosure?baseCurrency=CNY', { cache: false });
    renderRiskDisclosure(payload);
  } catch (error) {
    const dashboardEl = document.getElementById('portfolioRiskDashboard');
    const categoriesEl = document.getElementById('portfolioRiskCategories');
    const message = esc(error?.message || error);
    if (dashboardEl) {
      dashboardEl.innerHTML = dataStateHtml({
        tone: 'error',
        title: '风险披露读取失败',
        detail: message,
      });
    }
    if (categoriesEl) {
      categoriesEl.innerHTML = dataStateHtml({
        tone: 'error',
        title: '风险清单不可用',
        detail: message,
        compact: true,
      });
    }
    return;
    if (dashboardEl) dashboardEl.innerHTML = `<div class="lookthroughEmpty">风险披露读取失败：${message}</div>`;
    if (categoriesEl) categoriesEl.innerHTML = '';
  }
}

normalizeRiskSectionTitles();
bindRiskDisclosureEvents();
afterInvestmentStartup(loadPortfolioRiskDisclosure, 'risk').catch(() => {});
