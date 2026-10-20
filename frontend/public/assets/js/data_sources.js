import { jget } from './api.js?v=20260606-refresh-v3';
import { setActiveNav } from './ui.js?v=20260620-design-system-v2';
import { loadSyncCenter } from './sync_center.js?v=20260620-design-system-v3';

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

function renderQuality(portfolio) {
  const el = document.getElementById('dataQuality');
  if (!el) return;
  const quality = portfolio?.data_quality;
  if (!quality) {
    el.innerHTML = '<div class="muted">暂无统一数据质量信息</div>';
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
  const empty = new Set(missing.components_without_rows || []);
  const keys = Array.from(new Set([...Object.keys(labels), ...Object.keys(coverage), ...Object.keys(freshness)]));
  const healthyCount = keys.filter((key) => coverage[key]?.has_data !== false && !empty.has(key)).length;
  const actionKeys = keys.filter((key) => empty.has(key) || coverage[key]?.has_data === false);
  const actionHtml = actionKeys.length
    ? actionKeys.map((key) => {
      const action = actionLinks[key] || { label: '检查数据源', href: 'data_sources.html#dataQuality' };
      return `<a class="sourceActionLink" href="${esc(action.href)}">${esc(action.label)}</a>`;
    }).join('')
    : '<span class="muted">当前无阻塞动作</span>';
  el.innerHTML = `
    <div class="sourceQualitySummary">
      <div><span>覆盖范围</span><strong>${fmtCount(healthyCount)} / ${fmtCount(keys.length)}</strong></div>
      <div><span>行动提示</span><strong>${fmtCount(actionKeys.length)}</strong></div>
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
                <td>${statusTag(ok ? '正常' : '缺数据', ok)}</td>
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
