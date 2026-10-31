import { jget } from './api.js?v=20261031-risk-summary-retired-v1';
import { renderDataState, setActiveNav } from './ui.js?v=20261031-risk-summary-retired-v1';
import { loadSyncCenter } from './sync_center.js?v=20261031-risk-summary-retired-v1';

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

export function issuesHtml(quality, accounts = []) {
  const summary = quality?.summary;
  if (!quality || !Array.isArray(quality.checks) || !summary || !['ok', 'warning', 'error'].includes(summary.status) ||
      !Number.isInteger(summary.warning_count) || !Number.isInteger(summary.error_count) ||
      summary.warning_count < 0 || summary.error_count < 0 || quality.checks.some(check => !check || !check.status)) return '<p>数据状态暂不可用，请稍后重试。</p>';
  const checks = quality.checks.filter(check => check.status !== 'ok');
  if (!checks.length) return summary.status === 'ok' && summary.warning_count === 0 && summary.error_count === 0 ? '<p>当前没有待处理问题。</p>' : '<p>数据状态暂不可用，请稍后重试。</p>';
  const names = new Map((Array.isArray(accounts) ? accounts : []).map(account => [account.account_code, account.display_name || account.account_code]));
  return checks.map(check => {
    let title = check.message || check.id || '数据状态异常';
    let description = '打开数据诊断，核对原因后处理。';
    let href = 'data_sources.html';
    let action = '查看数据诊断';
    if (check.id === 'account_balance_coverage') {
      title = '部分账户缺少余额确认';
      const missing = Array.isArray(check.details?.missing_accounts) ? check.details.missing_accounts : [];
      const accounts = missing.map(code => names.get(code) || code);
      description = `${accounts.length ? accounts.join('、') + '。' : ''}核对最新余额和日期，然后在账户管理中确认。`;
      href = 'accounts.html#overview'; action = '核对账户';
    } else if (check.id === 'valuation_series_freshness') {
      title = '估值曲线日期落后';
      const detail = check.details || {};
      description = `估值序列最新 ${detail.valuation_series_latest_as_of || '未知'}，持仓最新 ${detail.investment_positions_latest_as_of || '未知'}。先核对数据来源和日期，确认缺口后再处理。`;
      href = 'data_sources.html#dataQuality'; action = '检查估值数据';
    }
    return `<section class="issueRow"><h2>${esc(title)}</h2><p>${esc(description)}</p><a href="${esc(href)}">${esc(action)} ›</a></section>`;
  }).join('');
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
  const el = document.body.classList.contains('issues-only') ? document.getElementById('issuesContent') : document.getElementById('err');
  if (!el) return;
  el.style.display = 'block';
  el.textContent = String(err?.message || err);
}

async function boot() {
  setActiveNav();
  if (new URLSearchParams(window.location.search).get('view') === 'issues') {
    document.body.classList.add('issues-only');
    document.getElementById('issuesView').hidden = false;
    const [portfolio, overview] = await Promise.all([
      jget('/api/portfolio/data-quality?baseCurrency=CNY').catch(() => null),
      jget('/api/accounts/overview').catch(() => null),
    ]);
    document.getElementById('issuesContent').innerHTML = issuesHtml(portfolio?.data_quality, overview?.accounts);
    return;
  }
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

if (typeof window !== 'undefined') boot().catch(showError);
