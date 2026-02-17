import { jget, jpost } from './api.js';
import { setActiveNav, kpi } from './ui.js';
import { showError } from './utils.js';

// ==================== 格式化 ====================

function fmtNum(x, digits = 2) {
  if (x == null || x === '') return '—';
  const n = Number(x);
  if (Number.isNaN(n)) return String(x);
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtPct(x) {
  if (x == null) return '—';
  const n = Number(x);
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function pctClass(x) {
  const n = Number(x);
  if (n > 0) return 'color:var(--good)';
  if (n < 0) return 'color:var(--bad)';
  return '';
}

// ==================== Chart.js 实例管理 ====================

let equityChartInstance = null;
let drawdownChartInstance = null;

// ==================== 参数收集 ====================

function isV2Selected() {
  return document.getElementById('stratV2')?.checked === true;
}

function collectParams() {
  const isCustom = document.getElementById('poolCustom')?.checked;

  const base = {
    start_date: document.getElementById('startDate')?.value || '2025-10-01',
    end_date: document.getElementById('endDate')?.value || '2026-02-01',
    initial_capital: Number(document.getElementById('capital')?.value) || 100000,
    risk_per_trade_pct: (Number(document.getElementById('riskPct')?.value) || 2) / 100,
    risk_neutral_pct: (Number(document.getElementById('riskNeutral')?.value) || 1) / 100,
    max_positions: Number(document.getElementById('maxPos')?.value) || 3,
    max_position_value_pct: (Number(document.getElementById('maxValuePct')?.value) || 50) / 100,
    stop_atr_mult: Number(document.getElementById('stopAtr')?.value) || 2.5,
    trailing_atr_mult: Number(document.getElementById('trailingAtr')?.value) || 3.0,
    // 突破参数
    breakout_lookback: Number(document.getElementById('breakoutLookback')?.value) || 20,
    volume_mult: Number(document.getElementById('volumeMult')?.value) || 1.2,
    // 金字塔
    pyramid_enabled: document.getElementById('pyramidEnabled')?.checked ?? true,
    pyramid_max_adds: Number(document.getElementById('pyramidMaxAdds')?.value) || 2,
    pyramid_risk_frac: (Number(document.getElementById('pyramidRiskFrac')?.value) || 50) / 100,
    pyramid_trigger_r: Number(document.getElementById('pyramidTriggerR')?.value) || 1.0,
  };

  // V2 Risk Protocol 参数
  if (isV2Selected()) {
    base.rp_normal_risk = (Number(document.getElementById('rpNormalRisk')?.value) || 2.5) / 100;
    base.rp_degraded_risk = (Number(document.getElementById('rpDegradedRisk')?.value) || 1.5) / 100;
    base.rp_conservative_risk = (Number(document.getElementById('rpConservativeRisk')?.value) || 1) / 100;
    base.rp_streak_threshold = Number(document.getElementById('rpStreakThreshold')?.value) || 12;
    base.rp_streak_trades = 20;
    base.rp_dd_enter = -(Number(document.getElementById('rpDdEnter')?.value) || 25);
    base.rp_dd_exit = -15;
    base.rp_neg_months = 9;
    base.rp_capital_lock_mult = Number(document.getElementById('rpCapitalLockMult')?.value) || 2.0;
    base.rp_capital_lock_r_reduction = 0.003;
    base.rp_conservative_top_pct = 0.20;
  }

  if (isCustom) {
    const raw = document.getElementById('customSymbols')?.value || '';
    const syms = raw.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);
    return { ...base, pool_source: 'custom', symbols: syms.length ? syms : null };
  }

  const interval = Number(document.getElementById('screenInterval')?.value) || 5;
  return { ...base, pool_source: 'dynamic', screen_interval_days: interval };
}

// ==================== 运行回测 ====================

async function handleRunBacktest() {
  const btn = document.getElementById('btnRun');
  const statusEl = document.getElementById('runStatus');
  if (!btn) return;

  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '回测中...';
  btn.style.opacity = '0.6';
  statusEl.style.display = 'block';
  statusEl.innerHTML = '<div class="muted">正在下载数据并执行回测，预计 30-60 秒...</div>';

  // 隐藏上次结果
  ['summaryCard', 'reportCard', 'rpEventsCard', 'equityCard', 'drawdownCard', 'weeklyCard', 'tradesCard', 'poolChangesCard'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });

  try {
    const params = collectParams();
    const useV2 = isV2Selected();
    const endpoint = useV2 ? '/api/backtest/run_v2' : '/api/backtest/run';
    const result = await jpost(endpoint, params);

    if (result.error) {
      statusEl.innerHTML = `<div style="color:var(--bad)">${result.error}</div>`;
      return;
    }

    statusEl.style.display = 'none';
    renderSummary(result.summary, result.params);
    renderReport(result.report);
    if (useV2) {
      renderRpEvents(result.risk_protocol_events, result.report?.risk_protocol);
    }
    renderEquityChart(result.equity_curve, result.benchmark_curve);
    renderDrawdownChart(result.equity_curve);
    renderPoolChanges(result.pool_changes);
    renderWeeklyTable(result.weekly_snapshots);
    renderTradeTable(result.trades);

  } catch (e) {
    statusEl.innerHTML = `<div style="color:var(--bad)">回测失败: ${String(e)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
    btn.style.opacity = '1';
  }
}

// ==================== 结果概览 ====================

function renderSummary(s, p) {
  if (!s || !s.final_equity) return;

  const card = document.getElementById('summaryCard');
  card.style.display = 'block';

  const poolLabel = p?.pool_source === 'dynamic'
    ? `动态筛选 · ${p.symbols_count}只候选`
    : `${p?.pool_source || 'db'} · ${p?.symbols_count || '?'}只`;
  document.getElementById('summaryMeta').textContent =
    `${s.start_date} ~ ${s.end_date} · ${s.trading_days} 交易日 · ${poolLabel}`;

  const retDir = s.total_return_pct >= 0 ? 'up' : 'down';
  const ddDir = 'down';

  const prDir = (s.payoff_ratio ?? 0) >= 2 ? 'up' : (s.payoff_ratio ?? 0) >= 1 ? '' : 'down';

  document.getElementById('summaryKpi').innerHTML = [
    kpi('总收益', fmtPct(s.total_return_pct), `¥${fmtNum(s.total_pnl, 0)}`, retDir),
    kpi('最大回撤', fmtPct(s.max_drawdown_pct), '', ddDir),
    kpi('盈亏比', fmtNum(s.payoff_ratio, 2), `均赢¥${fmtNum(s.avg_win, 0)} / 均亏¥${fmtNum(s.avg_loss, 0)}`, prDir),
    kpi('胜率', `${s.win_rate}%`, `${s.wins}胜 ${s.losses}负`, s.win_rate >= 50 ? 'up' : 'down'),
    kpi('交易次数', `${s.buys_count} 买 / ${s.sells_count} 卖`, '', ''),
    kpi('期末权益', `¥${fmtNum(s.final_equity, 0)}`, `初始¥${fmtNum(s.initial_capital, 0)}`, retDir),
  ].join('');
}

// ==================== 分析报告 ====================

function renderReport(report) {
  if (!report || !report.sections) return;

  const card = document.getElementById('reportCard');
  card.style.display = 'block';

  const gradeColors = { A: 'var(--good)', B: '#58a6ff', C: '#d29921', D: 'var(--bad)' };
  const gradeColor = gradeColors[report.grade] || '#8b949e';
  document.getElementById('reportGrade').innerHTML =
    `<span style="font-size:var(--fs-lg);font-weight:800;color:${gradeColor}">${report.grade}</span>` +
    `<span style="margin-left:8px">超额 ${report.alpha >= 0 ? '+' : ''}${report.alpha}% · 年化 ${report.ann_ret >= 0 ? '+' : ''}${report.ann_ret}%</span>`;

  document.getElementById('reportBody').innerHTML = report.sections.map(s => {
    const isImprove = s.title === '改进建议';
    return `
      <div style="margin-bottom:12px">
        <div style="font-weight:600;font-size:var(--fs-sm);margin-bottom:4px;color:var(--fg)">${s.title}</div>
        <div style="font-size:var(--fs-xs);line-height:1.7;color:${isImprove ? '#d29921' : 'var(--muted)'}">${s.content}</div>
      </div>`;
  }).join('');
}

// ==================== 权益曲线 ====================

function renderEquityChart(curve, benchmark) {
  if (!curve || curve.length === 0) return;

  document.getElementById('equityCard').style.display = 'block';

  if (equityChartInstance) equityChartInstance.destroy();

  // 构建基准数据 (按日期对齐)
  const bmMap = {};
  if (benchmark && benchmark.length > 0) {
    benchmark.forEach(p => { bmMap[p.date] = p.equity; });
  }
  const hasBenchmark = Object.keys(bmMap).length > 0;

  const datasets = [{
    label: '策略权益',
    data: curve.map(p => p.equity),
    borderColor: '#58a6ff',
    backgroundColor: 'rgba(88,166,255,0.08)',
    fill: true,
    borderWidth: 2,
    pointRadius: 0,
    tension: 0.1,
  }];

  if (hasBenchmark) {
    datasets.push({
      label: '上证指数',
      data: curve.map(p => bmMap[p.date] ?? null),
      borderColor: 'rgba(139,148,158,0.6)',
      backgroundColor: 'transparent',
      fill: false,
      borderWidth: 1.5,
      borderDash: [6, 3],
      pointRadius: 0,
      tension: 0.1,
    });
  }

  const ctx = document.getElementById('equityChart').getContext('2d');
  equityChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: curve.map(p => p.date),
      datasets,
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: hasBenchmark, labels: { color: '#8b949e', font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ¥${fmtNum(ctx.parsed.y, 0)}`,
          },
        },
      },
      scales: {
        x: {
          ticks: { maxTicksLimit: 10, color: '#6e7681', font: { size: 11 } },
          grid: { color: 'rgba(110,118,129,0.15)' },
        },
        y: {
          ticks: {
            color: '#6e7681', font: { size: 11 },
            callback: v => '¥' + (v / 1000).toFixed(0) + 'k',
          },
          grid: { color: 'rgba(110,118,129,0.15)' },
        },
      },
    },
  });
}

// ==================== 回撤曲线 ====================

function renderDrawdownChart(curve) {
  if (!curve || curve.length === 0) return;

  document.getElementById('drawdownCard').style.display = 'block';

  if (drawdownChartInstance) drawdownChartInstance.destroy();

  const ctx = document.getElementById('drawdownChart').getContext('2d');
  drawdownChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: curve.map(p => p.date),
      datasets: [{
        label: '回撤',
        data: curve.map(p => p.drawdown_pct),
        borderColor: '#f85149',
        backgroundColor: 'rgba(248,81,73,0.12)',
        fill: 'origin',
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.1,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ctx.parsed.y.toFixed(2) + '%',
          },
        },
      },
      scales: {
        x: {
          ticks: { maxTicksLimit: 10, color: '#6e7681', font: { size: 11 } },
          grid: { color: 'rgba(110,118,129,0.15)' },
        },
        y: {
          max: 0,
          ticks: {
            color: '#6e7681', font: { size: 11 },
            callback: v => v.toFixed(0) + '%',
          },
          grid: { color: 'rgba(110,118,129,0.15)' },
        },
      },
    },
  });
}

// ==================== 池变更记录 ====================

function renderPoolChanges(changes) {
  if (!changes || changes.length === 0) return;

  const card = document.getElementById('poolChangesCard');
  card.style.display = 'block';

  const h3 = document.querySelector('#poolChangesToggle h3');
  if (h3) h3.textContent = `股票池变更 (${changes.length}次筛选) ▾`;

  const tbody = document.getElementById('poolChangesRows');
  tbody.innerHTML = changes.map(c => {
    const addedList = (c.added || []).map(a => a.name).join(', ');
    const removedList = (c.removed || []).map(r => r.name).join(', ');

    const addedHtml = addedList
      ? `<span style="color:var(--good)">+${c.added.length}</span> <span class="muted" style="font-size:var(--fs-xs)">${addedList}</span>`
      : '<span class="muted">—</span>';
    const removedHtml = removedList
      ? `<span style="color:var(--bad)">-${c.removed.length}</span> <span class="muted" style="font-size:var(--fs-xs)">${removedList}</span>`
      : '<span class="muted">—</span>';

    return `
      <tr>
        <td class="nowrap mono">${c.date}</td>
        <td class="num">${c.active_count}</td>
        <td>${addedHtml}</td>
        <td>${removedHtml}</td>
      </tr>`;
  }).join('');
}

// ==================== 每周快照 ====================

function renderWeeklyTable(snapshots) {
  if (!snapshots || snapshots.length === 0) return;

  document.getElementById('weeklyCard').style.display = 'block';

  const tbody = document.getElementById('weeklyRows');
  tbody.innerHTML = snapshots.map(s => `
    <tr>
      <td class="nowrap mono">${s.date}</td>
      <td class="num mono">¥${fmtNum(s.equity, 0)}</td>
      <td class="num mono" style="${pctClass(s.return_pct)}">${fmtPct(s.return_pct)}</td>
      <td class="num mono" style="${pctClass(s.drawdown_pct)}">${fmtPct(s.drawdown_pct)}</td>
      <td class="num">${s.positions}</td>
      <td class="num mono">¥${fmtNum(s.cash, 0)}</td>
    </tr>
  `).join('');
}

// ==================== Risk Protocol Events (V2) ====================

function renderRpEvents(events, rpSummary) {
  const card = document.getElementById('rpEventsCard');
  if (!card) return;

  // 有 rpSummary 时总是显示 (即使 events 为空)
  if (!rpSummary && (!events || events.length === 0)) return;

  card.style.display = 'block';

  // KPI 摘要
  const kpiEl = document.getElementById('rpSummaryKpi');
  if (kpiEl && rpSummary) {
    const modeColor = rpSummary.degraded_pct > 30 ? 'down' : (rpSummary.conservative_pct > 0 ? 'down' : '');
    kpiEl.innerHTML = [
      kpi('有效R均值', rpSummary.avg_r_effective + '%', `锁减 ${rpSummary.r_reduction}%`, ''),
      kpi('NORMAL', rpSummary.normal_pct + '%', '', 'up'),
      kpi('DEGRADED', rpSummary.degraded_pct + '%', '', rpSummary.degraded_pct > 20 ? 'down' : ''),
      kpi('CONSERVATIVE', rpSummary.conservative_pct + '%', '', rpSummary.conservative_pct > 0 ? 'down' : ''),
      kpi('拦截入场', rpSummary.blocked_entries, '', rpSummary.blocked_entries > 0 ? '' : ''),
      kpi('拦截加仓', rpSummary.blocked_pyramids, '', rpSummary.blocked_pyramids > 0 ? '' : ''),
    ].join('');
  }

  // 事件表
  const tbody = document.getElementById('rpEventsRows');
  if (tbody && events && events.length > 0) {
    const eventColors = {
      'RP_ENTER_DEGRADED': 'background:rgba(210,153,34,.15);color:#d29921',
      'RP_ENTER_CONSERVATIVE': 'background:rgba(248,81,73,.15);color:#f85149',
      'RP_EXIT_DEGRADED': 'background:rgba(63,185,80,.15);color:#3fb950',
      'RP_CAPITAL_LOCK': 'background:rgba(88,166,255,.15);color:#58a6ff',
    };

    tbody.innerHTML = events.map(e => {
      let tagStyle = '';
      for (const [prefix, style] of Object.entries(eventColors)) {
        if (e.event.startsWith(prefix)) { tagStyle = style; break; }
      }
      return `
        <tr>
          <td class="nowrap mono">${e.date}</td>
          <td><span class="tag" style="${tagStyle}">${e.event}</span></td>
          <td class="muted" style="font-size:var(--fs-xs)">${e.detail || ''}</td>
        </tr>`;
    }).join('');
  } else if (tbody) {
    tbody.innerHTML = '<tr><td colspan="3" class="muted" style="text-align:center">回测期间未触发任何 Risk Protocol 事件</td></tr>';
  }
}

// ==================== 交易明细 ====================

function renderTradeTable(trades) {
  if (!trades || trades.length === 0) return;

  document.getElementById('tradesCard').style.display = 'block';

  const tbody = document.getElementById('tradeRows');
  tbody.innerHTML = trades.map((t, i) => {
    const isPyramid = t.action === 'BUY' && (t.reason || '').includes('金字塔');
    let actionTag;
    if (t.action === 'SELL') {
      actionTag = '<span class="tag data">SELL</span>';
    } else if (isPyramid) {
      actionTag = '<span class="tag" style="background:rgba(210,153,34,.15);color:#d29921">PYRAMID</span>';
    } else {
      actionTag = '<span class="tag risk">BUY</span>';
    }

    const pnlHtml = t.action === 'SELL'
      ? `<span style="${pctClass(t.pnl)}">¥${fmtNum(t.pnl, 0)}</span>`
      : '—';

    const reasonHtml = t.reason || (t.action === 'BUY' ? '突破信号' : '');

    // 决策链 (BUY 可展开)
    let reasoningRow = '';
    if (t.action === 'BUY' && t.reasoning) {
      const r = t.reasoning;
      const rpLine = r.risk_mode ? `<div><strong>5. RP模式</strong> ${r.risk_mode}</div>` : '';
      reasoningRow = `
        <tr id="reason-${i}" style="display:none">
          <td colspan="8" style="padding:8px 16px;background:rgba(88,166,255,0.04)">
            <div style="font-size:var(--fs-xs);line-height:1.6">
              <div><strong>1. 突破信号</strong> ${r.breakout || ''}</div>
              <div><strong>2. 市场环境</strong> ${r.regime || ''}</div>
              <div><strong>3. 入场/止损</strong> ${r.entry_stop || ''}</div>
              <div><strong>4. 仓位</strong> ${r.position || ''}</div>
              ${rpLine}
            </div>
          </td>
        </tr>`;
    }

    const expandAttr = t.action === 'BUY' && t.reasoning
      ? `style="cursor:pointer" onclick="document.getElementById('reason-${i}').style.display=document.getElementById('reason-${i}').style.display==='none'?'':'none'"`
      : '';

    return `
      <tr ${expandAttr}>
        <td class="nowrap mono">${t.date}</td>
        <td>${actionTag}</td>
        <td class="nowrap">${t.name}</td>
        <td class="num mono">¥${fmtNum(t.price)}</td>
        <td class="num mono">${t.shares}</td>
        <td class="num mono">¥${fmtNum(t.amount, 0)}</td>
        <td class="num mono">${pnlHtml}</td>
        <td class="muted">${reasonHtml}${t.action === 'BUY' && t.reasoning ? ' <span style="opacity:0.5">▸</span>' : ''}</td>
      </tr>
      ${reasoningRow}`;
  }).join('');
}

// ==================== 初始化 ====================

async function main() {
  setActiveNav();

  // 运行按钮
  document.getElementById('btnRun')?.addEventListener('click', handleRunBacktest);

  // 金字塔折叠
  document.getElementById('pyramidToggle')?.addEventListener('click', () => {
    const body = document.getElementById('pyramidBody');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    document.querySelector('#pyramidToggle .muted').textContent =
      open ? '金字塔加仓 ▸' : '金字塔加仓 ▾';
  });

  // 股票池折叠
  document.getElementById('poolToggle')?.addEventListener('click', () => {
    const body = document.getElementById('poolBody');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    document.querySelector('#poolToggle .muted').textContent =
      open ? '股票池选择 ▸' : '股票池选择 ▾';
  });

  // 股票池模式切换
  document.querySelectorAll('input[name="poolMode"]').forEach(radio => {
    radio.addEventListener('change', () => {
      const isCustom = document.getElementById('poolCustom')?.checked;
      document.getElementById('poolDynamicPanel').style.display = isCustom ? 'none' : 'block';
      document.getElementById('poolCustomPanel').style.display = isCustom ? 'block' : 'none';
    });
  });

  // 策略切换 (V1/V2)
  document.querySelectorAll('input[name="strategy"]').forEach(radio => {
    radio.addEventListener('change', () => {
      const v2 = isV2Selected();
      const rpSec = document.getElementById('rpSection');
      if (rpSec) rpSec.style.display = v2 ? 'block' : 'none';
      const desc = document.getElementById('stratDesc');
      if (desc) {
        desc.textContent = v2
          ? 'HV Alpha V2 · Risk Protocol · NORMAL/DEGRADED/CONSERVATIVE'
          : 'HV Alpha Model · 20日突破 + 金字塔加仓 + 3ATR移动止损';
      }
    });
  });

  // Risk Protocol 折叠
  document.getElementById('rpToggle')?.addEventListener('click', () => {
    const body = document.getElementById('rpBody');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    document.querySelector('#rpToggle .muted').textContent =
      open ? 'Risk Protocol ▸' : 'Risk Protocol ▾';
  });

  // RP Events 折叠
  document.getElementById('rpEventsToggle')?.addEventListener('click', () => {
    const body = document.getElementById('rpEventsBody');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
  });

  // 可折叠面板
  document.getElementById('poolChangesToggle')?.addEventListener('click', () => {
    const body = document.getElementById('poolChangesBody');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
  });

  document.getElementById('weeklyToggle')?.addEventListener('click', () => {
    const body = document.getElementById('weeklyBody');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    const h3 = document.querySelector('#weeklyToggle h3');
    if (h3) h3.textContent = open ? '每周市值快照 ▸' : '每周市值快照 ▾';
  });

  document.getElementById('tradesToggle')?.addEventListener('click', () => {
    const body = document.getElementById('tradesBody');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    const h3 = document.querySelector('#tradesToggle h3');
    if (h3) h3.textContent = open ? '交易明细 ▸' : '交易明细 ▾';
  });
}

main().catch(err => {
  showError('加载失败：' + String(err));
});
