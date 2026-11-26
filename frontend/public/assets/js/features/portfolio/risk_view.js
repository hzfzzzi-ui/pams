import {dataStateHtml} from '../../components/data_state.js';

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

function fmtWan(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  const sign = num < 0 ? '-' : '';
  const abs = Math.abs(num);
  if (abs >= 10000) return `${sign}¥${(abs / 10000).toFixed(1)}万`;
  return `${sign}¥${Math.round(abs).toLocaleString('zh-CN')}`;
}

function fmtPct1(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return `${(num * 100).toFixed(1)}%`;
}

function diagnosisSectionHtml(diagnosis, scope = 'all') {
  if (!diagnosis) return '';
  const activeScope = scope === 'ex_esop' && diagnosis.has_esop ? 'ex_esop' : 'all';
  const d = diagnosis[activeScope] || diagnosis.all;
  if (!d) return '';
  const liq = d.liquidity || {};
  const stress = d.stress || {};
  const eq20 = stress.equity_drop_20 || {};
  const fx5 = stress.fx_drop_5 || {};
  const levText = d.leverage_ratio > 0 ? `${Number(d.leverage_ratio).toFixed(2)}x` : '1.00x';
  const levSub = activeScope === 'all' && d.ibkr_leverage
    ? `盈透 ${Number(d.ibkr_leverage).toFixed(2)}x`
    : `可交易净值 ${fmtWan(d.net_value_cny)}`;
  const debtSub = d.total_debt_cny > 0
    ? `月息 ${fmtWan(d.estimated_monthly_interest_cny)} · 负债 ${fmtWan(d.total_debt_cny)}`
    : '无显著融资负债';
  const highPct = Math.max(0, Number(liq.high_pct || 0) * 100);
  const midPct = Math.max(0, Number(liq.mid_pct || 0) * 100);
  const lockPct = Math.max(0, Number(liq.locked_pct || 0) * 100);
  const eqBar = Math.min(100, Math.abs(Number(eq20.impact_nav_pct || 0)) * 250);
  const fxBar = Math.min(100, Math.abs(Number(fx5.impact_nav_pct || 0)) * 250);
  return `
    <div class="riskDiagnosisWrap">
      ${diagnosis.has_esop ? `
        <div class="riskScopeRow">
          <div class="riskScopeSwitch" role="tablist" aria-label="风控统计口径">
            <button type="button" role="tab" class="${activeScope === 'all' ? 'active' : ''}" aria-selected="${activeScope === 'all'}" data-risk-scope="all">全口径</button>
            <button type="button" role="tab" class="${activeScope === 'ex_esop' ? 'active' : ''}" aria-selected="${activeScope === 'ex_esop'}" data-risk-scope="ex_esop">剔除 ESOP</button>
          </div>
          <span class="riskScopeMeta">${activeScope === 'ex_esop' ? `净值 ${fmtWan(d.net_value_cny)}` : `ESOP ${fmtWan(diagnosis.esop_value_cny)}`}</span>
        </div>
      ` : ''}
      <div class="riskDiagnosisGrid">
        <div class="riskDiagnosisTile">
          <span>${activeScope === 'ex_esop' ? '二级市场杠杆' : '组合总杠杆'}</span>
          <strong class="${d.leverage_ratio >= 1.5 ? 'is-warn' : ''}">${esc(levText)}</strong>
          <small>${esc(levSub)}</small>
        </div>
        <div class="riskDiagnosisTile">
          <span>融资年息（估）</span>
          <strong class="${d.estimated_annual_interest_cny >= 10000 ? 'is-warn' : ''}">${esc(fmtWan(d.estimated_annual_interest_cny))}</strong>
          <small>${esc(debtSub)}</small>
        </div>
        <div class="riskDiagnosisTile">
          <span>最大单一标的</span>
          <strong class="${d.top1_pct >= 0.3 ? 'is-risk' : d.top1_pct >= 0.15 ? 'is-warn' : ''}">${esc(fmtPct1(d.top1_pct))}</strong>
          <small title="${esc(d.top1_name || '')}">${esc(cleanAssetName(d.top1_name))}</small>
        </div>
        <div class="riskDiagnosisTile">
          <span>权益敞口占比</span>
          <strong>${esc(fmtPct1(d.equity_pct))}</strong>
          <small>总敞口 ${esc(fmtWan(d.gross_exposure_cny))}</small>
        </div>
      </div>
      <div class="riskLiqChart">
        <div class="riskLiqHead">
          <span>流动性结构</span>
          <small>高流动 ${esc(fmtPct1(liq.high_pct))} · T+1 ${esc(fmtPct1(liq.mid_pct))}${activeScope === 'all' ? ` · 锁定 ${esc(fmtPct1(liq.locked_pct))}` : ''}</small>
        </div>
        <div class="riskLiqBar" aria-hidden="true">
          ${highPct > 0 ? `<i style="width:${highPct.toFixed(1)}%;background:#38bdf8"></i>` : ''}
          ${midPct > 0 ? `<i style="width:${midPct.toFixed(1)}%;background:#818cf8"></i>` : ''}
          ${lockPct > 0 ? `<i style="width:${lockPct.toFixed(1)}%;background:rgba(148,163,184,0.45)"></i>` : ''}
        </div>
      </div>
      <div class="riskStressStrip">
        <div class="riskStressItem">
          <div class="riskStressRow">
            <span>${esc(eq20.label || '权益/科技 -20%')}</span>
            <strong class="is-risk">${esc(fmtWan(eq20.impact_cny))} <em>${esc(fmtPct1(eq20.impact_nav_pct))} 净值</em></strong>
          </div>
          <div class="riskStressTrack"><i class="is-risk" style="width:${eqBar.toFixed(1)}%"></i></div>
        </div>
        <div class="riskStressItem">
          <div class="riskStressRow">
            <span>${esc(fx5.label || '外币汇率 -5%')}</span>
            <strong class="is-warn">${esc(fmtWan(fx5.impact_cny))} <em>${esc(fmtPct1(fx5.impact_nav_pct))} 净值</em></strong>
          </div>
          <div class="riskStressTrack"><i class="is-warn" style="width:${fxBar.toFixed(1)}%"></i></div>
        </div>
      </div>
      <div class="riskDiagnosisNote">${esc(d.interest_note || '')}</div>
    </div>
  `;
}

function dashboardView(payload, scope = 'all') {
  const dashboard = payload?.dashboard || {};
  const tone = dashboard.status_tone || 'info';
  const severity = dashboardSeverity(dashboard);
  const counts = severity.counts;
  const pct = clamp((Number(dashboard?.score ?? 0) / clamp(dashboard?.max_score ?? 100, 1, 999)) * 100);
  const radius = 38;
  const circumference = 2 * Math.PI * radius;
  const dashOffset = (circumference * (1 - pct / 100)).toFixed(2);

  return `
    <section class="portfolioRiskDashboard ${esc(tone)}">
      <div class="portfolioRiskDashboardMain">
        <div class="portfolioRiskRingWrap" aria-label="风险评分 ${esc(severity.scoreLabel)}${esc(severity.maxScoreLabel)}">
          <svg class="portfolioRiskRing" viewBox="0 0 92 92" aria-hidden="true">
            <circle class="portfolioRiskRingTrack" cx="46" cy="46" r="${radius}" />
            <circle
              class="portfolioRiskRingArc ${esc(tone)}"
              cx="46"
              cy="46"
              r="${radius}"
              stroke-dasharray="${circumference.toFixed(2)}"
              stroke-dashoffset="${dashOffset}"
            />
          </svg>
          <div class="portfolioRiskRingCenter">
            <strong>${esc(severity.scoreLabel)}</strong>
            <span>${esc(severity.maxScoreLabel)}</span>
          </div>
        </div>
        <div class="portfolioRiskDashboardBody">
          <div class="portfolioRiskDashboardTop">
            <div class="portfolioRiskStatusLine">
              <span class="portfolioRiskStatusDot ${esc(tone)}" aria-hidden="true"></span>
              <strong>${esc(dashboard.status_label || toneLabel(tone))}</strong>
              <small>综合风险评分</small>
            </div>
          </div>
          <div class="portfolioRiskDashboardStats">
            <span class="risk"><em>高风险</em><b>${esc(counts.risk)}</b></span>
            <span class="warn"><em>偏高</em><b>${esc(counts.warn)}</b></span>
            <span class="info"><em>观察</em><b>${esc(counts.info)}</b></span>
            <span class="ok"><em>正常</em><b>${esc(counts.ok)}</b></span>
          </div>
        </div>
      </div>
      ${diagnosisSectionHtml(payload?.diagnosis, scope)}
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

function ruleBlock(item, category) {
  const rule = item.rule || {};
  const tone = item.tone || 'info';
  const linked = rule.linked_config_rule || category?.name || '风险披露';
  const chips = thresholdChipData(item);
  const action = compactAdjustment(rule.adjustment_direction, tone);
  const actionLabel = tone === 'ok' ? '状态' : tone === 'risk' ? '建议' : '提示';
  return `
    <div class="portfolioRiskCompactRule">
      <div class="portfolioRiskThresholdLine">
        ${chips.map((chip) => `<span class="is-${esc(chip.tone)}">${esc(chip.label)} <b>${esc(chip.value)}</b></span>`).join('<i aria-hidden="true">·</i>')}
      </div>
      <div class="portfolioRiskRuleMeta">
        <span>配置 <b>${esc(linked)}</b></span>
        <span class="${esc(itemState(tone))}">${esc(actionLabel)} <b>${esc(action)}</b></span>
      </div>
    </div>
  `;
}

function inlineChecklistItem(item, category) {
  const tone = item.tone || 'info';
  const state = itemState(tone);
  const formula = ruleFormula(item);
  return `
    <section class="portfolioRiskInlineItem ${state}">
      <div class="portfolioRiskInlineTop">
        <div class="portfolioRiskInlineName">
          <strong>${esc(item.name || '--')}</strong>
          <small title="${esc(formula)}">${esc(formula)}</small>
        </div>
        <div class="portfolioRiskInlineVal">
          <span>${esc(item.weight_label || '--')}</span>
          <em class="portfolioRiskInlineBadge ${esc(tone)}">${esc(itemStatusLabel(tone))}</em>
        </div>
      </div>
      ${ruleBlock(item, category)}
      ${sourceChips(item.sources || [])}
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

function checklistCard(category, index, expandedRiskCategoryId) {
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
          <div class="portfolioRiskChecklistRight">
            <em>${esc(checklist.status_label || toneLabel(tone))}</em>
            <i class="portfolioRiskChevron" aria-hidden="true">›</i>
          </div>
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
      </button>
      ${isExpanded ? expandedChecklist(category) : ''}
    </section>
  `;
}

export function riskCategoriesHtml(currentCategories, expandedRiskCategoryId) {
  if (!currentCategories.length) return dataStateHtml({
    tone: 'empty', title: '暂无风险检查项',
    detail: '等待风险披露接口返回七类 checklist。', compact: true,
  });
  return currentCategories.map((category, index) => checklistCard(category, index, expandedRiskCategoryId)).join('')
    || '<div class="lookthroughEmpty">风险披露数据为空</div>';
}

export function riskErrorHtml(error) {
  // Preserve the existing error-label escaping in this structural migration.
  const message = esc(error?.message || error);
  return {
    dashboard: dataStateHtml({tone: 'error', title: '风险披露读取失败', detail: message}),
    categories: dataStateHtml({tone: 'error', title: '风险清单不可用', detail: message, compact: true}),
  };
}

export {dashboardView};
