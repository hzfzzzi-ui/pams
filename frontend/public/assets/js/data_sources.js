import { jget } from './api.js?v=20260606-refresh-v3';
import { renderDataState, setActiveNav } from './ui.js?v=20260622-data-state-a11y-v1';
import { loadSyncCenter } from './sync_center.js?v=20261023-ibkr-publication-v1';

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function fmtCount(value) {
  return Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}

function statusTag(text, ok) {
  return `<span class="tag ${ok ? 'data' : 'warn'}">${esc(text)}</span>`;
}

export function renderQuality(portfolio) {
  const el = document.getElementById('dataQuality');
  if (!el) return;
  const quality = portfolio?.data_quality;
  if (!quality) {
    renderDataState(el, {
      tone: 'empty',
      title: '暂无统一数据质量信息',
      detail: '等待 /api/portfolio/data-quality 返回覆盖率、缺口和行动提示。',
      compact: true,
    });
    return;
  }
  const labels = {
    account_balances: '账户余额',
    investment_positions: '投资持仓',
    investment_transactions: '投资交易',
    cash_transactions: '现金流水',
    transfer_matches: '内部转账',
    fx_rates: '汇率',
    valuation_series: '估值曲线',
  };
  const actionLinks = {
    account_balances: { label: '补录账户余额', href: 'ledger.html#accountIssueStrip' },
    transfer_matches: { label: '核准内部转账', href: 'ledger.html#transfers' },
    investment_positions: { label: '刷新投资持仓', href: 'investments.html' },
    investment_transactions: { label: '核对投资交易', href: 'investments.html#investment-ledger' },
    cash_transactions: { label: '补录现金流水', href: 'ledger.html' },
    fx_rates: { label: '检查汇率', href: 'data_sources.html#dataQuality' },
    valuation_series: { label: '检查估值曲线', href: 'data_sources.html#dataQuality' },
  };
  const coverage = quality.coverage || {};
  const freshness = quality.freshness || {};
  const missing = quality.missing || {};
  const checkComponents = {
    account_balance_coverage: 'account_balances',
    position_freshness: 'investment_positions',
    valuation_series_freshness: 'valuation_series',
    fx_rate_coverage: 'fx_rates',
  };
  const checkLabels = {
    component_coverage: '存在缺失的数据集',
    account_balance_coverage: '部分账户缺少余额确认',
    position_freshness: '部分持仓缺少日期',
    valuation_series_freshness: '估值曲线早于当前持仓',
    fx_rate_coverage: '部分币种缺少汇率',
  };
  const checks = (quality.checks || []).filter((check) => check.status !== 'ok');
  const warningKeys = new Set(checks.flatMap((check) => (
    checkComponents[check.id] ? [checkComponents[check.id]] : check.details?.missing_components || []
  )));
  const empty = new Set(missing.components_without_rows || []);
  const keys = Array.from(new Set([...Object.keys(labels), ...Object.keys(coverage), ...Object.keys(freshness)]));
  const healthyCount = keys.filter((key) => coverage[key]?.has_data !== false && !empty.has(key)).length;
  const actionKeys = keys.filter((key) => warningKeys.has(key) || empty.has(key) || coverage[key]?.has_data === false);
  const checkHtml = checks.map((check) => `<div>${statusTag(check.severity === 'error' ? '异常' : '待核准', false)} ${esc(checkLabels[check.id] || check.message || check.id)}</div>`).join('');
  const actionHtml = actionKeys.length || checks.length
    ? checkHtml + actionKeys.map((key) => {
      const action = actionLinks[key] || { label: '检查数据源', href: 'data_sources.html#dataQuality' };
      return `<a class="sourceActionLink" href="${esc(action.href)}">${esc(action.label)}</a>`;
    }).join('')
    : '<span class="muted">当前无阻塞动作</span>';
  el.innerHTML = `
    <div class="sourceQualitySummary">
      <div><span>覆盖范围</span><strong>${fmtCount(healthyCount)} / ${fmtCount(keys.length)}</strong></div>
      <div><span>行动提示</span><strong>${fmtCount(checks.length || actionKeys.length)}</strong></div>
    </div>
    <div class="tableWrap">
      <table class="qualityMatrix">
        <thead>
          <tr><th>数据集</th><th class="num">记录数</th><th>最新日期</th><th>状态</th></tr>
        </thead>
        <tbody>
          ${keys.map((key) => {
            const cov = coverage[key] || {};
            const fresh = freshness[key] || {};
            const ok = cov.has_data !== false && !empty.has(key);
            return `
              <tr>
                <td>${esc(labels[key] || key)}</td>
                <td class="num mono">${fmtCount(cov.rows)}</td>
                <td class="mono">${esc(fresh.latest_as_of || '--')}</td>
                <td>${statusTag(!ok ? '缺数据' : warningKeys.has(key) ? '待核准' : '正常', ok && !warningKeys.has(key))}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    </div>
    <div class="sourceActionHints">
      <strong>行动提示</strong>
      <div>${actionHtml}</div>
    </div>
  `;
}

function showError(err) {
  const el = document.getElementById('err');
  if (!el) return;
  el.style.display = 'block';
  el.textContent = String(err?.message || err);
}

async function boot() {
  setActiveNav();
  const statusLine = document.getElementById('statusLine');
  if (statusLine) statusLine.classList.add('syncSummaryStrip');
  const [portfolio] = await Promise.all([
    jget('/api/portfolio/data-quality?baseCurrency=CNY').catch(() => ({ data_quality: null })),
    loadSyncCenter({
      summaryEl: statusLine,
      workflowEl: document.getElementById('todayActions'),
      rowsEl: document.getElementById('sourceList'),
      tableBodyEl: document.getElementById('sourceGrid'),
      statusEl: document.getElementById('manualSyncStatus'),
    }),
  ]);
  document.getElementById('asof').textContent = portfolio.as_of || new Date().toISOString().slice(0, 10);
  renderQuality(portfolio);
}

boot().catch(showError);
