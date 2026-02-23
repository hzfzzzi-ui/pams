import { setActiveNav } from './ui.js';

const state = {
  validationStatus: 'idle',
  lastValidation: null,
  lastValidationKey: null,
  lastRun: null,
  lastExperiment: null,
  experimentJobId: null,
  experimentRunning: false,
};

function prettyPresetName(preset) {
  const p = String(preset || '');
  if (p === 'TREND_ACCEL_BRAKE_VALIDATE_v1') return '3.3 M2+M3（强趋势加速+趋势破位刹车）';
  if (p === 'TREND_BRAKE_CONFIRM_VALIDATE_v1') return '3.4 M3 Confirm（趋势破位确认退出）';
  if (p === 'MAX_POSITION_EXPAND_VALIDATE_v1') return '5.2 Max Position Expand（扩容验证）';
  if (p === 'STRONG_ONLY_VALIDATE_v1') return '5.3 Strong Only（强趋势集中火力）';
  if (p === 'MULTIFACTOR_RANKING_VALIDATE_v1') return '6.5 多因子排名裁剪（TOPK5/10）';
  if (p === 'MULTIFACTOR_RANKING_WALKFORWARD_v1') return '6.6 多因子排名稳健性（WF+分阶段）';
  if (p === 'MF_RANK_REGIME_GATE_VALIDATE_v1') return '6.7 多因子风险门控（仅RiskOn启用）';
  if (p === 'MF_RANK_STRONG_GATE_VALIDATE_v1') return '6.8 多因子强门控（仅强趋势启用）';
  if (p === 'TREND_QUALITY_VALIDATE_v1') return '6.9 趋势质量验证（Slope+Continuity）';
  if (p === 'MULTIFACTOR_INCREMENTAL_V1') return '6.1 多因子增量实验';
  if (p === 'MULTIFACTOR_INCREMENTAL_V1_6_1B') return '6.1B 多因子限速版';
  if (p === 'MULTIFACTOR_INCREMENTAL_V1_6_1A') return '6.1A 多因子快速版';
  if (p === 'MULTIFACTOR_BALANCED_v1') return '多因子融合 v1 [已废弃]';
  if (p === 'MULTIFACTOR_FUSION_COMPARE_v1') return '多因子融合模式对比 [已废弃]';
  return p;
}

function renderProgressHtml(label, pct, note = '') {
  const p = Math.max(0, Math.min(100, Number(pct || 0)));
  return `
    <div style="padding:var(--sp-3);background:rgba(88,166,255,.08);border-left:3px solid var(--accent);border-radius:var(--radius-sm)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span>${label}</span>
        <span class="mono">${p}%</span>
      </div>
      <div style="height:10px;background:var(--bg-surface);border:1px solid var(--line);border-radius:999px;overflow:hidden">
        <div style="height:100%;width:${p}%;background:linear-gradient(90deg,var(--accent),#7cb7ff);transition:width .3s ease"></div>
      </div>
      ${note ? `<div class="muted" style="margin-top:8px;font-size:var(--fs-sm)">${note}</div>` : ''}
    </div>
  `;
}

async function waitForJob(jobId, onProgress) {
  const maxPoll = 43200;
  let notFoundRetry = 0;
  for (let i = 0; i < maxPoll; i += 1) {
    let r;
    try {
      r = await fetch(`/api/backtest_v2/jobs/${encodeURIComponent(jobId)}`);
    } catch (_) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      continue;
    }
    if (!r.ok) {
      // Backend reload/restart may cause a short race for in-memory jobs; retry briefly on 404.
      if (r.status === 404 && notFoundRetry < 20) {
        notFoundRetry += 1;
        await new Promise((resolve) => setTimeout(resolve, 1000));
        continue;
      }
      if ((r.status === 502 || r.status === 503 || r.status === 504) && notFoundRetry < 120) {
        notFoundRetry += 1;
        await new Promise((resolve) => setTimeout(resolve, 1500));
        continue;
      }
      throw new Error(`任务状态查询失败: ${r.status} ${r.statusText}`);
    }
    notFoundRetry = 0;
    const j = await r.json();
    if (onProgress) onProgress(j);
    if (j.status === 'success') return j.result || null;
    if (j.status === 'failed') throw new Error(j.error || '任务执行失败');
    if (j.status === 'canceled') return { __canceled: true, message: j.message || '任务已取消' };
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error('任务超时，请稍后重试');
}

function updateExperimentButtons() {
  const btnRun = document.getElementById('btnRunExperiment');
  const btnCancel = document.getElementById('btnCancelExperiment');
  if (!btnRun || !btnCancel) return;
  btnCancel.style.display = state.experimentRunning ? 'inline-block' : 'none';
  btnCancel.disabled = !state.experimentRunning;
}

function fmtNum(v, digits = 2) {
  const n = Number(v ?? 0);
  if (Number.isNaN(n)) return '-';
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function pct(v) {
  return `${(Number(v ?? 0) * 100).toFixed(2)}%`;
}

const STAGE_LABELS = {
  universe_resolve: '1) 成分解析',
  data_load: '2) 数据加载',
  precompute: '3) 预计算',
  simulation: '4) 仿真执行',
  output_write: '5) 产物写盘',
  report_write: '6) 报告生成',
};

function formatStageSeconds(stageSeconds) {
  const src = stageSeconds && typeof stageSeconds === 'object' ? stageSeconds : {};
  const keys = Object.keys(src);
  if (!keys.length) return [];
  const ordered = ['universe_resolve', 'data_load', 'precompute', 'simulation', 'output_write', 'report_write'];
  const remaining = keys.filter((k) => !ordered.includes(k)).sort();
  const all = [...ordered.filter((k) => k in src), ...remaining];
  return all.map((k) => ({
    key: k,
    label: STAGE_LABELS[k] || k,
    seconds: Number(src[k] || 0),
  }));
}

function renderStageTimingInline(stageSeconds) {
  const items = formatStageSeconds(stageSeconds);
  if (!items.length) return '';
  return items.map((it) => `${it.label}=${it.seconds.toFixed(2)}s`).join(' · ');
}

function renderStageTimingHtml(stageSeconds) {
  const items = formatStageSeconds(stageSeconds);
  if (!items.length) return '';
  const rows = items.map((it) => `<tr><td style="padding:2px 8px 2px 0">${it.label}</td><td class="mono" style="text-align:right;padding:2px 0">${it.seconds.toFixed(2)}s</td></tr>`).join('');
  return `<div style="margin-top:8px"><table style="font-size:var(--fs-sm);color:var(--text-secondary)">${rows}</table></div>`;
}

function collectPayload() {
  const symbolsRaw = document.getElementById('symbols')?.value || '';
  const symbols = symbolsRaw.split(',').map((s) => s.trim()).filter(Boolean);
  return {
    start_date: document.getElementById('startDate')?.value || '2021-01-01',
    end_date: document.getElementById('endDate')?.value || '2026-02-01',
    benchmark: document.getElementById('benchmark')?.value || '000300.SH',
    compare_symbol: document.getElementById('compareSymbol')?.value || '000001.SH',
    symbols: symbols.length ? symbols : ['600000.SH', '000001.SZ'],
    initial_capital: Number(document.getElementById('initialCapital')?.value || 100000),
    fast: Number(document.getElementById('fast')?.value || 5),
    slow: Number(document.getElementById('slow')?.value || 20),
    order_size: Number(document.getElementById('orderSize')?.value || 1000),
  };
}

function collectValidationParams() {
  const p = collectPayload();
  return {
    symbols: p.symbols,
    start: p.start_date,
    end: p.end_date,
    benchmark: p.benchmark,
    strict_suspended: !!document.getElementById('strictSuspended')?.checked,
    audit_dump: true,
    allow_cn_index_fallback: false,
  };
}

function validationKey(vp) {
  return JSON.stringify({
    symbols: [...vp.symbols].sort(),
    start: vp.start,
    end: vp.end,
    benchmark: vp.benchmark,
    strict_suspended: vp.strict_suspended,
  });
}

function updateRunGate() {
  const btnRun = document.getElementById('btnRun');
  const forceRun = !!document.getElementById('forceRun')?.checked;
  const gate = document.getElementById('runGateMsg');
  if (!btnRun || !gate) return;

  if (state.validationStatus === 'pass') {
    btnRun.disabled = false;
    gate.style.background = 'var(--glow-good)';
    gate.style.color = 'var(--good)';
    gate.textContent = `✓ 验收通过（request_id: ${state.lastValidation?.request_id || '-'}）`;
    return;
  }

  if (forceRun) {
    btnRun.disabled = false;
    gate.style.background = 'var(--glow-bad)';
    gate.style.color = 'var(--bad)';
    gate.textContent = `⚠ 未通过验收（状态: ${state.validationStatus}），已开启强制运行`;
    return;
  }

  btnRun.disabled = true;
  gate.style.background = 'var(--accent-dim)';
  gate.style.color = 'var(--text-secondary)';
  gate.textContent =
    state.validationStatus === 'stale'
      ? '⚠ 参数已变更，验收结果失效，请重新验收'
      : '请先点击"数据验收"';
}

function markStaleIfNeeded() {
  const key = validationKey(collectValidationParams());
  if (state.lastValidationKey && state.lastValidationKey !== key && ['pass', 'fail'].includes(state.validationStatus)) {
    state.validationStatus = 'stale';
  }
  updateRunGate();
}

function renderValidation(result) {
  const card = document.getElementById('validationCard');
  const summary = document.getElementById('validationSummary');
  const checksBox = document.getElementById('validationChecks');
  const samplesBox = document.getElementById('validationSamples');
  if (!card || !summary || !checksBox || !samplesBox) return;

  card.style.display = 'block';
  const passText = result.pass ? 'PASS' : 'FAIL';
  const passColor = result.pass ? 'var(--good)' : 'var(--bad)';
  const lim = result.checks?.limits || {};
  const warnCount = (result.warnings || []).length;
  summary.innerHTML = `
    <span style="font-weight:700;color:${passColor}">${passText}</span>
    <span style="margin:0 8px;color:var(--line)">·</span>
    <span>warnings: ${warnCount}</span>
    <span style="margin:0 8px;color:var(--line)">·</span>
    <span>high>limit_up: ${lim.high_gt_limit_up ?? 0}</span>
    <span style="margin:0 8px;color:var(--line)">·</span>
    <span style="font-family:monospace;font-size:var(--fs-sm)">request_id: ${result.request_id}</span>
  `;

  checksBox.innerHTML = '';
  const checks = result.checks || {};
  Object.entries(checks).forEach(([k, v]) => {
    const d = document.createElement('div');
    d.style.marginBottom = 'var(--sp-3)';
    d.style.padding = 'var(--sp-2)';
    d.style.background = 'var(--bg-surface)';
    d.style.borderRadius = 'var(--radius-sm)';
    d.style.borderLeft = `3px solid ${v && v.pass !== false ? 'var(--good)' : 'var(--bad)'}`;
    d.innerHTML = `
      <div style="font-weight:600;margin-bottom:var(--sp-1);color:${v && v.pass !== false ? 'var(--good)' : 'var(--bad)'}; word-break:break-word">${k}</div>
      <pre style="white-space:pre-wrap;word-break:break-word;margin:0;font-size:var(--fs-sm);color:var(--text-secondary);overflow-x:auto;max-width:100%">${JSON.stringify(v, null, 2)}</pre>
    `;
    checksBox.appendChild(d);
  });

  samplesBox.innerHTML = `<pre style="white-space:pre-wrap;margin:0;font-size:var(--fs-sm);font-family:monospace">${JSON.stringify(result.samples || {}, null, 2)}</pre>`;
}

function deriveSummaryFromReport(resp) {
  const report = resp.report || {};
  const summary = report.summary || {};
  const cost = report.cost_breakdown || {};

  return {
    run_id: resp.run_id || '',
    date_range: { start: '-', end: '-' },
    universe: { symbols_count: 0, symbols_sample: [] },
    benchmark: { symbol: '-', rows: 0, source: '-' },
    strategy: { name: report.strategy_name || 'unknown', params: report.strategy_params || {} },
    kpis: {
      trading_days: summary.trading_days || 0,
      orders_total: summary.orders || 0,
      trades_count: summary.trades || 0,
      filled_rate: 0,
      total_return: Number(summary.total_return_pct || 0) / 100.0,
      max_drawdown: Math.abs(Number(summary.max_drawdown_pct || 0)) / 100.0,
      cagr: 0,
      volatility: 0,
      sharpe: 0,
    },
    costs: {
      commission_total: Number(cost.commission_total || 0),
      stamp_tax_total: Number(cost.stamp_tax_total || 0),
      fees_total: Number(cost.fees_total || 0),
      slippage_attribution_total: Number(cost.slippage_cost_total || 0),
    },
    reasons: [],
    audit: {
      no_lookahead_enforced: !!(report.audit_rules?.no_lookahead_enforced ?? true),
      slippage_mode: report.slippage_mode || 'price_adjustment_only',
      limit_source: report.audit_rules?.limit_source || '-',
      limit_price_epsilon: report.audit_rules?.limit_price_epsilon || null,
      validation: report.data_validation || { request_id: null, passed: null, warnings: [] },
    },
    recommendations: [
      { code: 'OLD_REPORT', title: '旧报告版本', detail: '未检测到 report_summary.json，建议重新运行回测生成摘要。' },
      { code: 'CHECK_COST', title: '检查成本结构', detail: '重点关注手续费与滑点归因。' },
      { code: 'CHECK_RISK', title: '检查回撤区间', detail: '建议按回撤区间复盘交易原因。' },
    ],
    artifacts: {
      report_json: resp.files?.report || 'report.json',
      report_summary_json: resp.files?.report_summary || 'report_summary.json',
      equity_curve_csv: resp.files?.equity || 'equity_curve.csv',
      benchmark_curve_csv: resp.files?.benchmark_curve || 'benchmark_curve.csv',
      orders_csv: resp.files?.orders || 'orders.csv',
      trades_csv: resp.files?.trades || 'trades.csv',
      trade_pnl_csv: resp.files?.trade_pnl || 'trade_pnl.csv',
      conclusions_10_json: resp.files?.conclusions_10 || 'conclusions_10.json',
      warnings_json: resp.files?.warnings || 'warnings.json',
    },
    chart: resp.chart || {},
    trade_concentration: resp.trade_concentration || {},
    conclusions_10: resp.conclusions_10 || null,
  };
}

function buildConclusion(s) {
  const r = Number(s.kpis?.total_return || 0);
  const dd = Number(s.kpis?.max_drawdown || 0);
  if (r < 0 && dd > 0.15) return '回测亏损且回撤偏大，策略可能在震荡期被反复触发。';
  if (r > 0 && dd < 0.1) return '收益与回撤匹配较好，策略表现相对稳健。';
  return '收益与风险处于中性区间，建议结合原因分布继续优化。';
}

function renderDashboard(resp) {
  const resultCard = document.getElementById('resultCard');
  const runMeta = document.getElementById('runMeta');
  if (!resultCard || !runMeta) return;
  resultCard.style.display = 'block';

  const s = resp.report_summary || deriveSummaryFromReport(resp);
  runMeta.textContent = `run_id: ${resp.run_id || s.run_id || '-'} · strategy: ${s.strategy?.name || '-'} · trades: ${s.kpis?.trades_count ?? 0}`;

  const perf = document.getElementById('cardPerf');
  const cost = document.getElementById('cardCost');
  const reason = document.getElementById('cardReason');
  const audit = document.getElementById('cardAudit');
  const reco = document.getElementById('cardReco');
  const links = document.getElementById('artifactLinks');
  const concentration = document.getElementById('cardConcentration');
  if (!perf || !cost || !reason || !audit || !reco || !links || !concentration) return;

  perf.innerHTML = [
    `<div style="margin-bottom:var(--sp-2)">总收益: <b style="color:${Number(s.kpis?.total_return || 0) >= 0 ? 'var(--good)' : 'var(--bad)'}">${pct(s.kpis?.total_return)}</b></div>`,
    `<div style="margin-bottom:var(--sp-2)">最大回撤: <b style="color:var(--bad)">${pct(s.kpis?.max_drawdown)}</b></div>`,
    `<div style="margin-bottom:var(--sp-2)">CAGR: <b>${pct(s.kpis?.cagr)}</b> · Sharpe: <b>${fmtNum(s.kpis?.sharpe, 2)}</b> · 波动率: <b>${pct(s.kpis?.volatility)}</b></div>`,
    `<div style="margin-bottom:var(--sp-2)">交易日: ${s.kpis?.trading_days ?? 0} · 交易数: ${s.kpis?.trades_count ?? 0} · 成交率: ${pct(s.kpis?.filled_rate)}</div>`,
    `<div style="margin-top:var(--sp-2);padding-top:var(--sp-2);border-top:1px solid var(--line);font-size:var(--fs-sm);color:var(--text-secondary)">${buildConclusion(s)}</div>`,
  ].join('');

  const avgCost = Number(s.kpis?.trades_count || 0) > 0 ? Number(s.costs?.fees_total || 0) / Number(s.kpis.trades_count) : 0;
  cost.innerHTML = [
    `<div style="margin-bottom:var(--sp-2)">佣金: ¥${fmtNum(s.costs?.commission_total, 2)} · 印花税: ¥${fmtNum(s.costs?.stamp_tax_total, 2)}</div>`,
    `<div style="margin-bottom:var(--sp-2)">费用合计: <b style="color:var(--bad)">¥${fmtNum(s.costs?.fees_total, 2)}</b></div>`,
    `<div style="margin-bottom:var(--sp-2)">滑点归因: ¥${fmtNum(s.costs?.slippage_attribution_total, 2)}<span style="color:var(--text-secondary);font-size:var(--fs-sm)">（不计入fees_total）</span></div>`,
    `<div style="margin-bottom:var(--sp-2)">单笔平均费用: ¥${fmtNum(avgCost, 2)}</div>`,
    Number(s.kpis?.filled_rate || 0) < 0.7 ? '<div style="padding-top:var(--sp-2);border-top:1px solid var(--line);font-size:var(--fs-sm);color:var(--warn)">⚠ 成交率偏低，可能受涨跌停/停牌/容量拒单影响</div>' : '',
  ].join('');

  const reasons = s.reasons || [];
  reason.innerHTML = reasons.length
    ? `<table class="tableWide"><thead><tr><th>reason_code</th><th class="num">orders</th><th class="num">trades</th><th class="num">win_rate</th><th class="num">avg_hold_days</th></tr></thead><tbody>${
      reasons.slice(0, 8).map((r) => `<tr><td>${r.reason_code}</td><td class="num">${r.orders}</td><td class="num">${r.trades}</td><td class="num">${r.win_rate == null ? '-' : pct(r.win_rate)}</td><td class="num">${r.avg_hold_days == null ? '-' : fmtNum(r.avg_hold_days, 2)}</td></tr>`).join('')
    }</tbody></table>`
    : '无交易/无原因统计。';

  const val = s.audit?.validation || {};
  const warnList = val.warnings || [];
  const passStatus = val.passed === false ? 'FAIL' : (val.passed === true ? 'PASS' : 'UNKNOWN');
  const passColor = val.passed === false ? 'var(--bad)' : (val.passed === true ? 'var(--good)' : 'var(--text-secondary)');
  audit.innerHTML = [
    `<div style="margin-bottom:var(--sp-2)">数据验收: <b style="color:${passColor}">${passStatus}</b> ${warnList.length > 0 ? `<span style="color:var(--warn);margin-left:8px">(${warnList.length} warnings)</span>` : ''}</div>`,
    `<div style="margin-bottom:var(--sp-1)">no_lookahead_enforced: <code style="color:${s.audit?.no_lookahead_enforced ? 'var(--good)' : 'var(--bad)'}; word-break:break-all">${s.audit?.no_lookahead_enforced ? 'true' : 'false'}</code></div>`,
    `<div style="margin-bottom:var(--sp-1)">limit_source: <code style="word-break:break-all">${s.audit?.limit_source || '-'}</code></div>`,
    `<div style="margin-bottom:var(--sp-1)">limit_price_epsilon: <code style="word-break:break-all">${JSON.stringify(s.audit?.limit_price_epsilon ?? null)}</code></div>`,
    `<div style="margin-bottom:var(--sp-1)">slippage_mode: <code style="word-break:break-all">${s.audit?.slippage_mode || '-'}</code></div>`,
    warnList.length ? `<details style="margin-top:var(--sp-2);padding-top:var(--sp-2);border-top:1px solid var(--line)"><summary style="cursor:pointer;color:var(--warn);font-weight:600">告警详情 (${warnList.length})</summary><pre style="white-space:pre-wrap;word-break:break-word;margin-top:var(--sp-2);font-size:var(--fs-sm);color:var(--text-secondary);overflow:auto;max-width:100%">${JSON.stringify(warnList.slice(0, 5), null, 2)}</pre></details>` : '',
  ].join('');

  reco.innerHTML = (s.recommendations || []).slice(0, 3).map((r, i) => `
    <div style="margin-bottom:${i < 2 ? 'var(--sp-3)' : '0'}">
      <div style="font-weight:600;margin-bottom:var(--sp-1);color:var(--accent)">${i + 1}. ${r.title}</div>
      <div style="color:var(--text-secondary);font-size:var(--fs-sm)">${r.detail}</div>
    </div>
  `).join('');

  const tc = resp.trade_concentration || s.trade_concentration || {};
  const hasData = tc.closed_trades != null && tc.closed_trades > 0;
  concentration.innerHTML = hasData ? [
    `<div style="margin-bottom:var(--sp-2)">闭合交易数: <b>${tc.closed_trades ?? 0}</b> ${tc.sample_hint === 'small_sample' ? '<span style="color:var(--warn);margin-left:8px">(样本偏少)</span>' : ''}</div>`,
    `<div style="margin-bottom:var(--sp-2)">Top 5%贡献: <b style="color:${(tc.top5_contribution_pct ?? 0) > 0.5 ? 'var(--warn)' : 'var(--good)'}">${tc.top5_contribution_pct == null ? 'N/A' : pct(tc.top5_contribution_pct)}</b></div>`,
    `<div style="margin-bottom:var(--sp-2)">Top 10%贡献: <b style="color:${(tc.top10_contribution_pct ?? 0) > 0.7 ? 'var(--warn)' : 'var(--good)'}">${tc.top10_contribution_pct == null ? 'N/A' : pct(tc.top10_contribution_pct)}</b></div>`,
    `<div style="padding-top:var(--sp-2);border-top:1px solid var(--line);font-size:var(--fs-sm);color:var(--text-secondary);word-break:break-word">sum_all: ${fmtNum(tc.sum_all, 2)} · sum_top5: ${fmtNum(tc.sum_top5, 2)} · sum_top10: ${fmtNum(tc.sum_top10, 2)}</div>`,
    `<div style="margin-top:var(--sp-2);font-size:var(--fs-sm)">详见: <code style="color:var(--accent);word-break:break-all">${(s.artifacts || {}).trade_pnl_csv || 'trade_pnl.csv'}</code></div>`,
  ].join('') : '<div style="color:var(--text-secondary)">暂无交易集中度数据</div>';

  renderBenchmarkChart(resp);
  renderConclusions(resp);

  const artifacts = s.artifacts || {};
  links.innerHTML = Object.entries(artifacts).map(([k, v]) => {
    const valText = String(v || '');
    return `
      <div style="margin-bottom:var(--sp-2);padding:var(--sp-2);background:var(--bg-surface);border-radius:var(--radius-sm)">
        <div style="font-weight:600;margin-bottom:var(--sp-1);color:var(--text-secondary);font-size:var(--fs-sm)">${k}</div>
        <div style="display:flex;align-items:center;gap:var(--sp-2)">
          <code style="flex:1;font-size:var(--fs-sm);color:var(--accent);overflow-x:auto;word-break:break-all;max-width:100%">${valText}</code>
          <button class="pill" data-copy="${encodeURIComponent(valText)}" style="flex-shrink:0;background:var(--card-hover);border:1px solid var(--line);padding:4px 10px;font-size:var(--fs-sm)">复制</button>
        </div>
      </div>
    `;
  }).join('');
  links.querySelectorAll('button[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const t = decodeURIComponent(btn.getAttribute('data-copy') || '');
      try {
        await navigator.clipboard.writeText(t);
        btn.textContent = '✓ 已复制';
        btn.style.background = 'var(--good)';
        btn.style.color = '#fff';
        setTimeout(() => {
          btn.textContent = '复制';
          btn.style.background = 'var(--card-hover)';
          btn.style.color = 'var(--text)';
        }, 1500);
      } catch (e) {
        btn.textContent = '复制失败';
        setTimeout(() => { btn.textContent = '复制'; }, 1500);
      }
    });
  });

  renderTrades(resp);
}

function renderBenchmarkChart(resp) {
  const box = document.getElementById('curveChart');
  if (!box) return;
  const rows = resp.benchmark_curve_preview || [];
  if (!rows.length) {
    box.textContent = '无 benchmark_curve 数据，无法绘图。';
    return;
  }
  const pts = rows
    .map((r) => ({
      date: String(r.date || ''),
      strategy: Number(r.strategy_nav),
      bench: Number(r.benchmark_nav),
    }))
    .filter((x) => Number.isFinite(x.strategy) && Number.isFinite(x.bench));
  if (!pts.length) {
    box.textContent = '净值对比数据为空。';
    return;
  }
  const width = 900;
  const height = 260;
  const pad = 28;
  const all = pts.flatMap((x) => [x.strategy, x.bench]);
  const minY = Math.min(...all);
  const maxY = Math.max(...all);
  const ySpan = Math.max(maxY - minY, 1e-9);
  const toX = (i) => pad + (i * (width - pad * 2)) / Math.max(pts.length - 1, 1);
  const toY = (v) => height - pad - ((v - minY) * (height - pad * 2)) / ySpan;
  const mkPath = (key) => pts.map((p, i) => `${toX(i)},${toY(p[key])}`).join(' ');
  box.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" style="width:100%;height:auto;background:#0f1720;border:1px solid #1f2a37;border-radius:8px">
      <polyline points="${mkPath('strategy')}" fill="none" stroke="#48a3ff" stroke-width="2"></polyline>
      <polyline points="${mkPath('bench')}" fill="none" stroke="#ffb347" stroke-width="2"></polyline>
      <text x="${pad}" y="${pad - 8}" fill="#9fb3c8" font-size="12">策略净值(蓝) / 指数净值(橙)</text>
      <text x="${pad}" y="${height - 6}" fill="#9fb3c8" font-size="11">${pts[0].date}</text>
      <text x="${width - 90}" y="${height - 6}" fill="#9fb3c8" font-size="11">${pts[pts.length - 1].date}</text>
    </svg>
  `;
}

function renderConclusions(resp) {
  const box = document.getElementById('conclusionsList');
  if (!box) return;
  const data = resp.conclusions_10 || resp.report_summary?.conclusions_10 || null;
  if (!data || !Array.isArray(data.items)) {
    box.textContent = '无 conclusions_10 数据。';
    return;
  }
  box.innerHTML = data.items.map((it) => `
    <details style="margin-bottom:8px">
      <summary><b>${it.id}. ${it.title}</b> <span style="color:${it.status === 'FAIL' ? 'var(--bad)' : (it.status === 'WARN' ? '#f59e0b' : 'var(--good)')}">[${it.status}]</span> ${it.value || ''}</summary>
      <div style="margin-top:6px">${it.detail || ''}</div>
    </details>
  `).join('');
}

async function copyConclusionsText() {
  const data = state.lastRun?.conclusions_10 || state.lastRun?.report_summary?.conclusions_10;
  if (!data || !Array.isArray(data.items)) {
    window.alert('当前没有可复制的结论文本。');
    return;
  }
  const lines = [`run_id: ${data.run_id || state.lastRun?.run_id || ''}`];
  data.items.forEach((it) => {
    lines.push(`${it.id}. [${it.status}] ${it.title} | ${it.value || ''} | ${it.detail || ''}`);
  });
  if (data.suggested_next_change) {
    lines.push(`next: ${data.suggested_next_change.code || ''} | ${data.suggested_next_change.reason || ''}`);
  }
  try {
    await navigator.clipboard.writeText(lines.join('\n'));
    const btn = document.getElementById('btnCopyConclusions');
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✓ 已复制';
      btn.style.background = 'var(--good)';
      btn.style.color = '#fff';
      setTimeout(() => {
        btn.textContent = orig;
        btn.style.background = 'var(--card-hover)';
        btn.style.color = 'var(--text)';
      }, 1500);
    }
  } catch (e) {
    window.alert('复制失败: ' + e.message);
  }
}

function renderTrades(resp) {
  const rows = resp.trades_preview || [];
  const card = document.getElementById('tradesCard');
  const tbody = document.getElementById('tradeRows');
  if (!card || !tbody) return;
  if (!rows.length) {
    card.style.display = 'none';
    return;
  }

  card.style.display = 'block';
  tbody.innerHTML = rows.map((r) => `<tr><td class="mono">${r.trade_date || ''}</td><td>${r.symbol || ''}</td><td>${r.side || ''}</td><td class="num mono">${r.qty ?? ''}</td><td class="num mono">${fmtNum(r.price, 4)}</td><td class="num mono">${fmtNum(r.commission, 2)}</td><td class="num mono">${fmtNum(r.stamp_tax, 2)}</td><td class="num mono">${fmtNum(r.slippage, 4)}</td><td class="num mono">${fmtNum(r.net_amount, 2)}</td></tr>`).join('');
}

function renderExperimentMatrix(resp) {
  const card = document.getElementById('experimentCard');
  const meta = document.getElementById('experimentMeta');
  const tbody = document.getElementById('experimentRows');
  if (!card || !meta || !tbody) return;

  const rows = Array.isArray(resp?.rows) ? resp.rows : [];
  card.style.display = 'block';
  const bundleTag = resp.bundle ? ` · bundle: <code>${resp.bundle}</code>` : '';
  const bundleDir = resp.bundle_dir ? ` · bundle_dir: <code>${resp.bundle_dir}</code>` : '';
  const rp = resp?.run_profile_summary || {};
  const stageHintText = renderStageTimingInline(rp.stage_seconds || {});
  const stageHint = stageHintText ? ` · stages: <code>${stageHintText}</code>` : '';
  const workloadHint = (Number(rp.symbols_count || 0) > 0 || Number(rp.trading_days || 0) > 0)
    ? ` · workload: <code>symbols=${Number(rp.symbols_count || 0)}, days=${Number(rp.trading_days || 0)}, variants=${Number(rp.variant_count || 0)}</code>`
    : '';
  meta.innerHTML = `exp_id: <code>${resp.exp_id || '-'}</code> · preset: ${prettyPresetName(resp.preset || '-')}${bundleTag} · results: <code>${resp.results_dir || '-'}</code>${bundleDir}${workloadHint}${stageHint}`;

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="23" class="muted">无实验结果</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td>${r.group_id || ''}</td>
      <td title="strategy: ${(r.strategy_name_full || r.strategy_name || '').replaceAll('"', '&quot;')}\nparams: ${(r.params_json_full || r.params_json || '{}').replaceAll('"', '&quot;')}">${r.strategy_name || ''}</td>
      <td><span style="color:${r.status === 'OK' ? 'var(--good)' : 'var(--bad)'};font-weight:700">${r.status || '-'}</span></td>
      <td class="num mono">${r.total_return == null ? '-' : pct(r.total_return)}</td>
      <td class="num mono">${r.max_drawdown == null ? '-' : pct(r.max_drawdown)}</td>
      <td class="num mono">${r.sharpe == null ? '-' : fmtNum(r.sharpe, 2)}</td>
      <td class="num mono">${r.filled_rate == null ? '-' : pct(r.filled_rate)}</td>
      <td class="num mono">${r.raw_signals_total ?? '-'}</td>
      <td class="num mono" title="blocked_reasons_top3: ${String(r.blocked_reasons_top3 || '{}').replaceAll('"', '&quot;')}">${r.blocked_signals_total ?? 0}</td>
      <td class="num mono">${r.mode || '-'}</td>
      <td class="num mono">${r.atr_stop_k ?? '-'}</td>
      <td class="num mono">${r.atr_trail_k ?? '-'}</td>
      <td class="num mono" title="ATR_STOP_LOSS=${r.atr_stop_exit_count ?? 0}, ATR_TRAIL_EXIT=${r.atr_trail_exit_count ?? 0}">STOP ${r.atr_stop_exit_count ?? 0} / TRAIL ${r.atr_trail_exit_count ?? 0}</td>
      <td class="num mono">${r.atr_exit_rate == null ? '-' : pct(r.atr_exit_rate)}</td>
      <td class="num mono">${r.score == null ? '-' : fmtNum(r.score, 4)}</td>
      <td class="num mono">${r.trades_count ?? '-'}</td>
      <td class="num mono">${r.tradable_orders_total ?? r.orders_total ?? '-'}</td>
      <td class="num mono" title="rejected_reasons_top3: ${String(r.rejected_reasons_top3 || '{}').replaceAll('"', '&quot;')}">${r.rejected_orders_total ?? r.rejected_orders ?? '-'}</td>
      <td class="num mono" title="skipped_reasons_top3: ${String(r.skipped_reasons_top3 || '{}').replaceAll('"', '&quot;')}">${r.skipped_orders_total ?? r.skipped_orders ?? '-'}</td>
      <td class="num mono">${r.closed_trades ?? '-'}</td>
      <td class="num mono" title="${buildTop10Tooltip(r)}">${top10DisplayValue(r)}</td>
      <td class="mono">${r.run_id || '-'}</td>
      <td title="${(r.error_message || r.warning_notes || '').replaceAll('"', '&quot;')}" style="max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${r.error_message || r.warning_notes || '-'}</td>
    </tr>
  `).join('');
}

function top10DisplayValue(r) {
  const v = r.top10_profit_concentration_pct;
  if (v == null) return '—';
  return pct(v);
}

function buildTop10Tooltip(r) {
  const lines = [
    `closed_trades: ${r.closed_trades ?? '-'}`,
    `top10_count: ${r.top10_count ?? '-'}`,
    `sum_all_net_pnl: ${r.sum_all_net_pnl ?? '-'}`,
    `sum_top10_net_pnl: ${r.sum_top10_net_pnl ?? '-'}`,
    `top10_profit_concentration_pct: ${r.top10_profit_concentration_pct == null ? 'null' : pct(r.top10_profit_concentration_pct)}`,
    `top10_vs_total_net_pct(legacy): ${r.top10_vs_total_net_pct == null ? 'null' : pct(r.top10_vs_total_net_pct)}`,
  ];
  if (r.top10_profit_concentration_pct == null && r.top10_vs_total_net_pct != null) {
    lines.push('note: legacy metric only (UI default uses profit concentration)');
  }
  if (r.top10_offset_loss_pct != null) lines.push(`top10_offset_loss_pct: ${pct(r.top10_offset_loss_pct)}`);
  if (r.warning_notes) lines.push(`note: ${r.warning_notes}`);
  return lines.join('\n').replaceAll('"', '&quot;');
}

function buildValidateUrl(vp) {
  const q = new URLSearchParams();
  q.set('symbols', vp.symbols.join(','));
  q.set('start', vp.start);
  q.set('end', vp.end);
  q.set('benchmark', vp.benchmark);
  q.set('audit_dump', String(vp.audit_dump));
  q.set('strict_suspended', String(vp.strict_suspended));
  q.set('allow_cn_index_fallback', String(vp.allow_cn_index_fallback));
  return `/api/backtest_v2/validate_data?${q.toString()}`;
}

async function doValidate() {
  const status = document.getElementById('runStatus');
  const btnValidate = document.getElementById('btnValidate');
  if (!status || !btnValidate) return;

  // Add loading visual feedback
  const originalText = btnValidate.textContent;
  btnValidate.disabled = true;
  btnValidate.style.opacity = '0.7';
  btnValidate.innerHTML = '<span style="display:inline-flex;align-items:center;gap:8px"><span style="animation:spin 1s linear infinite;display:inline-block">⟳</span>验收中...</span>';

  state.validationStatus = 'loading';
  updateRunGate();
  status.style.display = 'block';
  status.innerHTML = '<div class="muted" style="animation:pulse 2s ease-in-out infinite">正在校验数据源...</div>';

  const vp = collectValidationParams();
  try {
    const res = await fetch(buildValidateUrl(vp), { method: 'GET' });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);

    const result = await res.json();
    state.lastValidation = result;
    state.lastValidationKey = validationKey(vp);
    state.validationStatus = result.pass ? 'pass' : 'fail';

    // Success animation
    btnValidate.style.opacity = '1';
    btnValidate.innerHTML = result.pass ?
      `<span style="color:var(--good)">✓ ${originalText}</span>` :
      `<span style="color:var(--warn)">⚠ ${originalText}</span>`;

    renderValidation(result);
    status.style.display = 'none';

    // Reset button after animation
    setTimeout(() => {
      btnValidate.textContent = originalText;
      btnValidate.disabled = false;
    }, 1500);
  } catch (e) {
    state.validationStatus = 'error';
    btnValidate.style.opacity = '1';
    btnValidate.innerHTML = `<span style="color:var(--bad)">✗ ${originalText}</span>`;
    status.innerHTML = `<div style="color:var(--bad);padding:var(--sp-3);background:rgba(248,81,73,.1);border-left:3px solid var(--bad);border-radius:var(--radius-sm)">数据验收失败: ${e.message || e}</div>`;

    setTimeout(() => {
      btnValidate.textContent = originalText;
      btnValidate.disabled = false;
    }, 2000);
  }
  updateRunGate();
}

async function runV2() {
  const btn = document.getElementById('btnRun');
  const status = document.getElementById('runStatus');
  if (!btn || !status) return;

  const forceRun = !!document.getElementById('forceRun')?.checked;
  if (state.validationStatus !== 'pass' && !forceRun) {
    updateRunGate();
    return;
  }

  if (state.validationStatus !== 'pass' && forceRun) {
    const ok = window.confirm('当前数据验收未通过，确认强制运行吗？');
    if (!ok) return;
  }

  const original = btn.textContent;
  btn.disabled = true;
  btn.style.opacity = '0.7';
  btn.innerHTML = '<span style="display:inline-flex;align-items:center;gap:8px"><span style="animation:spin 1s linear infinite;display:inline-block">⟳</span>运行中...</span>';
  status.style.display = 'block';
  status.innerHTML = renderProgressHtml('正在提交回测任务...', 0, '');

  try {
    const payload = {
      ...collectPayload(),
      force_run: forceRun,
      validation_request_id: state.lastValidation?.request_id || null,
      validation_passed: state.validationStatus === 'pass',
      validation_warnings: (state.lastValidation?.warnings || []).map((w) => (typeof w === 'string' ? w : JSON.stringify(w))),
    };

    const respRaw = await fetch('/api/backtest_v2/run_async', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!respRaw.ok) {
      let detail = `${respRaw.status} ${respRaw.statusText}`;
      try {
        const err = await respRaw.json();
        detail = err.detail || detail;
      } catch (_) {
        // ignore parse error
      }
      throw new Error(detail);
    }

    const queued = await respRaw.json();
    const jobId = queued.job_id;
    if (!jobId) throw new Error('未收到 job_id');
    const resp = await waitForJob(jobId, (j) => {
      status.innerHTML = renderProgressHtml('回测进行中...', j.progress_pct ?? 0, j.message || '');
    });
    state.lastRun = resp;

    // Success animation
    btn.style.opacity = '1';
    btn.innerHTML = '<span style="color:var(--good)">✓ 完成</span>';
    status.innerHTML = '<div style="color:var(--good);padding:var(--sp-3);background:rgba(63,185,80,.1);border-left:3px solid var(--good);border-radius:var(--radius-sm);animation:fadeIn .3s ease-out">✓ 回测完成，正在渲染报告...</div>';

    setTimeout(() => {
      status.style.display = 'none';
      btn.textContent = original;
      btn.disabled = false;
    }, 1000);

    renderDashboard(resp);
    switchTab('summary');
  } catch (e) {
    btn.style.opacity = '1';
    btn.innerHTML = '<span style="color:var(--bad)">✗ 失败</span>';
    status.innerHTML = `<div style="color:var(--bad);padding:var(--sp-3);background:rgba(248,81,73,.1);border-left:3px solid var(--bad);border-radius:var(--radius-sm)">回测失败: ${e.message || e}</div>`;

    setTimeout(() => {
      btn.textContent = original;
      btn.disabled = false;
    }, 2000);
  }
}

async function runExperiment() {
  const btn = document.getElementById('btnRunExperiment');
  const status = document.getElementById('experimentStatus');
  const preset = document.getElementById('experimentPreset')?.value || 'G1-G4';
  if (!btn || !status) return;

  const p = collectPayload();
  const original = btn.textContent;
  btn.disabled = true;
  btn.style.opacity = '0.7';
  btn.innerHTML = '<span style="display:inline-flex;align-items:center;gap:8px"><span style="animation:spin 1s linear infinite;display:inline-block">⟳</span>实验运行中...</span>';
  status.style.display = 'block';
  const runningText = preset === 'ATR_VALIDATE_BUNDLE_v1'
    ? '正在运行 Step1+Step2+Step3 一键验证链路，请稍候...'
    : `正在运行 ${prettyPresetName(preset)}，请稍候...`;
  status.innerHTML = renderProgressHtml(runningText, 0, '');
  state.experimentRunning = true;
  updateExperimentButtons();

  try {
    const now = new Date();
    const endDate = now.toISOString().slice(0, 10);
    const startDateObj = new Date(now);
    startDateObj.setFullYear(startDateObj.getFullYear() - 1);
    const startDate = startDateObj.toISOString().slice(0, 10);

    const payload = {
      preset,
      symbols: p.symbols,
      start_date: preset === 'ATR_PARAM_VALIDATE_UNIVOTE_V2' ? startDate : p.start_date,
      end_date: preset === 'ATR_PARAM_VALIDATE_UNIVOTE_V2' ? endDate : p.end_date,
      benchmark: p.benchmark,
      compare_symbol: p.compare_symbol,
      initial_capital: preset === 'ATR_PARAM_VALIDATE_UNIVOTE_V2' ? 1000000 : p.initial_capital,
    };
    if (preset === 'PORTFOLIO_FILL_VALIDATE_v1') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2023-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.universe_limit = 50;
    }
    if (preset === 'TREND_FILTER_VALIDATE_v1') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2023-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 50;
      payload.min_avg_turnover_20d = 100000000;
    }
    if (preset === 'TREND_TIER_VALIDATE_v1') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2021-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'TREND_ACCEL_BRAKE_VALIDATE_v1') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2021-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'TREND_BRAKE_CONFIRM_VALIDATE_v1') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2021-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'STRENGTH_RANK_VALIDATE_v1') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2025-02-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
    }
    if (preset === 'STRENGTH_RANK_VALIDATE_v2') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2024-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 200;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'STRENGTH_RANK_VALIDATE_v2A') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2024-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 3;
      payload.universe_limit = 200;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'STRENGTH_RANK_VALIDATE_v2B') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2024-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'FULL_RANGE_VALIDATE_v1') {
      const defaultUiStart = p.start_date === '2021-01-01';
      const defaultUiEnd = p.end_date === '2026-02-01';
      if (defaultUiStart && defaultUiEnd) {
        payload.start_date = '2021-01-01';
        payload.end_date = '2026-02-01';
      }
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'CANDIDATE_DIAG_VALIDATE_v1') {
      payload.start_date = '2013-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 3000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'SLOT_DEBUG_VALIDATE_v1') {
      payload.start_date = '2013-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'MAX_POSITION_EXPAND_VALIDATE_v1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.order_value = 10000;
      payload.univote_lot_mode = 'LOT100_MIN1LOT';
      payload.lot_size = 100;
      payload.fast_mode = true;
    }
    if (preset === 'STRONG_ONLY_VALIDATE_v1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.order_value = 10000;
      payload.univote_lot_mode = 'LOT100_MIN1LOT';
      payload.lot_size = 100;
      payload.fast_mode = true;
    }
    if (preset === 'ENGINE_PERF_COMPARE_v1') {
      payload.start_date = '2025-02-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 300;
      payload.min_avg_turnover_20d = 50000000;
      payload.fast_mode = true;
    }
    if (preset === 'ATR_PARAM_VALIDATE_UNIVOTE_V2') {
      payload.base_order_value = 10000;
      payload.univote_lot_mode = 'LOT100_MIN1LOT';
      payload.lot_size = 100;
      payload.universe_limit = 100;
    }
    if (preset === 'ATR_PORTFOLIO_VALIDATE_v1') {
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 100;
    }
    if (preset === 'MULTIFACTOR_INCREMENTAL_V1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'MULTIFACTOR_RANKING_VALIDATE_v1') {
      payload.start_date = '2023-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 300;
      payload.min_avg_turnover_20d = 100000000;
      payload.order_value = 10000;
      payload.univote_lot_mode = 'LOT100_MIN1LOT';
      payload.lot_size = 100;
      payload.fast_mode = true;
    }
    if (preset === 'MULTIFACTOR_RANKING_WALKFORWARD_v1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.order_value = 10000;
      payload.univote_lot_mode = 'LOT100_MIN1LOT';
      payload.lot_size = 100;
      payload.fast_mode = true;
    }
    if (preset === 'MF_RANK_REGIME_GATE_VALIDATE_v1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.order_value = 10000;
      payload.univote_lot_mode = 'LOT100_MIN1LOT';
      payload.lot_size = 100;
      payload.fast_mode = true;
    }
    if (preset === 'MF_RANK_STRONG_GATE_VALIDATE_v1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.order_value = 10000;
      payload.univote_lot_mode = 'LOT100_MIN1LOT';
      payload.lot_size = 100;
      payload.fast_mode = true;
    }
    if (preset === 'TREND_QUALITY_VALIDATE_v1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.order_value = 10000;
      payload.univote_lot_mode = 'LOT100_MIN1LOT';
      payload.lot_size = 100;
      payload.fast_mode = true;
    }
    if (preset === 'MULTIFACTOR_BALANCED_v1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    if (preset === 'MULTIFACTOR_FUSION_COMPARE_v1') {
      payload.start_date = '2021-01-01';
      payload.end_date = '2026-02-01';
      payload.initial_capital = 1000000;
      payload.max_positions = 10;
      payload.universe_limit = 1000;
      payload.min_avg_turnover_20d = 100000000;
      payload.fast_mode = true;
    }
    const raw = await fetch('/api/backtest_v2/experiment/run_async', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!raw.ok) {
      let detail = `${raw.status} ${raw.statusText}`;
      try {
        const err = await raw.json();
        detail = err.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const queued = await raw.json();
    const jobId = queued.job_id;
    if (!jobId) throw new Error('未收到 job_id');
    state.experimentJobId = jobId;
    const resp = await waitForJob(jobId, (j) => {
      status.innerHTML = renderProgressHtml(`实验进行中（${prettyPresetName(preset)}）...`, j.progress_pct ?? 0, j.message || '');
    });
    if (resp && resp.__canceled) {
      status.innerHTML = `<div style="color:var(--warn);padding:var(--sp-3);background:rgba(245,158,11,.12);border-left:3px solid var(--warn);border-radius:var(--radius-sm)">实验已取消</div>`;
      return;
    }
    state.lastExperiment = resp;
    renderExperimentMatrix(resp);
    if (resp.bundle) {
      const s1 = resp.step1?.results_dir || '-';
      const s2 = resp.step2?.results_dir || '-';
      const s3 = resp.step3?.results_dir || '-';
      status.innerHTML = `<div style="color:var(--good);padding:var(--sp-3);background:rgba(63,185,80,.1);border-left:3px solid var(--good);border-radius:var(--radius-sm)">✓ 一键验证完成<br><span style="font-size:var(--fs-sm);color:var(--text-secondary)">Step1: ${s1}<br>Step2: ${s2}<br>Step3: ${s3}</span></div>`;
    } else {
      const rp = resp?.run_profile_summary || {};
      const stageBox = renderStageTimingHtml(rp.stage_seconds || {});
      const wall = Number(rp.total_wall_seconds || 0);
      const wallLine = wall > 0 ? `<br><span style="font-size:var(--fs-sm);color:var(--text-secondary)">总耗时：${wall.toFixed(2)}s</span>` : '';
      const tips = Array.isArray(rp.optimization_tips) ? rp.optimization_tips : [];
      const tipsHtml = tips.length
        ? `<div style="margin-top:8px;font-size:var(--fs-sm);color:var(--text-secondary)">优化建议：${tips.map((t) => String(t)).join('；')}</div>`
        : '';
      status.innerHTML = `<div style="color:var(--good);padding:var(--sp-3);background:rgba(63,185,80,.1);border-left:3px solid var(--good);border-radius:var(--radius-sm)">✓ 对照实验完成${wallLine}${stageBox}${tipsHtml}</div>`;
    }
  } catch (e) {
    status.innerHTML = `<div style="color:var(--bad);padding:var(--sp-3);background:rgba(248,81,73,.1);border-left:3px solid var(--bad);border-radius:var(--radius-sm)">对照实验失败: ${e.message || e}</div>`;
  } finally {
    state.experimentRunning = false;
    state.experimentJobId = null;
    updateExperimentButtons();
    btn.disabled = false;
    btn.style.opacity = '1';
    btn.textContent = original;
  }
}

async function cancelExperiment() {
  const status = document.getElementById('experimentStatus');
  const jobId = state.experimentJobId;
  if (!jobId) return;
  try {
    const r = await fetch(`/api/backtest_v2/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    if (status) {
      status.style.display = 'block';
      status.innerHTML = '<div style="color:var(--warn);padding:var(--sp-3);background:rgba(245,158,11,.12);border-left:3px solid var(--warn);border-radius:var(--radius-sm)">已请求取消实验，正在停止...</div>';
    }
    const btnCancel = document.getElementById('btnCancelExperiment');
    if (btnCancel) btnCancel.disabled = true;
  } catch (e) {
    if (status) {
      status.style.display = 'block';
      status.innerHTML = `<div style="color:var(--bad);padding:var(--sp-3);background:rgba(248,81,73,.1);border-left:3px solid var(--bad);border-radius:var(--radius-sm)">取消失败: ${e.message || e}</div>`;
    }
  }
}

function switchTab(tab) {
  const summaryPanel = document.getElementById('summaryPanel');
  const drillPanel = document.getElementById('drillPanel');
  const tabSummary = document.getElementById('tabSummary');
  const tabDrill = document.getElementById('tabDrill');
  if (!summaryPanel || !drillPanel || !tabSummary || !tabDrill) return;

  const isSummary = tab === 'summary';
  summaryPanel.style.display = isSummary ? 'block' : 'none';
  drillPanel.style.display = isSummary ? 'none' : 'block';

  if (isSummary) {
    tabSummary.style.background = 'var(--accent)';
    tabSummary.style.color = '#000';
    tabSummary.style.border = 'none';
    tabDrill.style.background = 'var(--card-hover)';
    tabDrill.style.color = 'var(--text)';
    tabDrill.style.border = '1px solid var(--line)';
  } else {
    tabSummary.style.background = 'var(--card-hover)';
    tabSummary.style.color = 'var(--text)';
    tabSummary.style.border = '1px solid var(--line)';
    tabDrill.style.background = 'var(--accent)';
    tabDrill.style.color = '#000';
    tabDrill.style.border = 'none';
  }
}

function bindParamWatchers() {
  ['startDate', 'endDate', 'benchmark', 'compareSymbol', 'symbols', 'initialCapital', 'orderSize', 'fast', 'slow', 'strictSuspended'].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('change', markStaleIfNeeded);
    el.addEventListener('input', markStaleIfNeeded);
  });
  document.getElementById('forceRun')?.addEventListener('change', updateRunGate);
}

function main() {
  setActiveNav();
  bindParamWatchers();
  updateRunGate();
  document.getElementById('btnValidate')?.addEventListener('click', doValidate);
  document.getElementById('btnRun')?.addEventListener('click', runV2);
  document.getElementById('tabSummary')?.addEventListener('click', () => switchTab('summary'));
  document.getElementById('tabDrill')?.addEventListener('click', () => switchTab('drill'));
  document.getElementById('btnCopyConclusions')?.addEventListener('click', copyConclusionsText);
  document.getElementById('btnRunExperiment')?.addEventListener('click', runExperiment);
  document.getElementById('btnCancelExperiment')?.addEventListener('click', cancelExperiment);
  updateExperimentButtons();
}

main();
