import { jget } from './api.js?v=20260619-investment-hardening-v1';
import { dataStateHtml, openBottomSheet } from './ui.js?v=20260620-data-state-v1';

let currentRules = [];
let currentDrilldownRules = [];
let currentRebalance = [];
let currentGroups = [];
let expandedRuleIds = new Set();
let expandedSubRuleIds = new Set();
let currentAsOf = '--';

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function n(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function fmtPct(value, digits = 1) {
  if (!Number.isFinite(Number(value))) return '--';
  return `${(n(value) * 100).toFixed(digits)}%`;
}

function normalizeRule(row) {
  return {
    id: row.id,
    groupId: row.group_id || 'other',
    name: row.name,
    current: n(row.current),
    targetLow: row.target_low == null ? null : n(row.target_low),
    targetHigh: row.target_high == null ? null : n(row.target_high),
    hardLow: row.hard_low == null ? null : n(row.hard_low),
    hardHigh: row.hard_high == null ? null : n(row.hard_high),
    tone: row.tone || 'ok',
    status: row.status || '目标内',
    targetLabel: row.target_label || '--',
    limitLabel: row.limit_label || '--',
    currentAmountCny: n(row.current_amount_cny),
    currentAmountLabel: row.current_amount_label || '--',
    targetAmountLabel: row.target_amount_label || '--',
    limitAmountLabel: row.limit_amount_label || '--',
    detail: row.detail || '',
    children: Array.isArray(row.children) ? row.children : [],
  };
}

function childAmountTotal(children = []) {
  return children.reduce((sum, child) => sum + n(child.amount_cny), 0);
}

function normalizeGroup(row) {
  return {
    id: row.id || 'other',
    name: row.name || '其他',
    description: row.description || '',
  };
}

function targetBandStyle(rule, scaleMax) {
  const start = Math.max(n(rule.targetLow), 0);
  const end = Math.max(n(rule.targetHigh ?? rule.targetLow), start + 0.006);
  return `left:${Math.min(100, start / scaleMax * 100)}%;width:${Math.max(2, Math.min(100, (end - start) / scaleMax * 100))}%`;
}

function ruleTrack(rule) {
  const current = Math.max(0, n(rule.current));
  const relevantMax = Math.max(current, n(rule.hardHigh), n(rule.hardLow), n(rule.targetHigh), n(rule.targetLow), 0.08);
  const scaleMax = relevantMax * 1.18;
  const fill = Math.min(100, current / scaleMax * 100);
  const marker = Math.min(100, current / scaleMax * 100);
  return `
    <div class="portfolioRuleTrack" aria-hidden="true">
      <span class="portfolioRuleTarget" style="${targetBandStyle(rule, scaleMax)}"></span>
      <span class="portfolioRuleFill ${esc(rule.tone)}" style="width:${fill}%"></span>
      <i style="left:${marker}%"></i>
    </div>
  `;
}

function findRule(id) {
  return [...currentRules, ...currentDrilldownRules].find((item) => item.id === id);
}

function ruleExpanded(rule, level) {
  return level === 'sub' ? expandedSubRuleIds.has(rule.id) : expandedRuleIds.has(rule.id);
}

function ruleRow(rule, { level = 'top' } = {}) {
  const canExpand = rule.children.length > 0;
  const expanded = ruleExpanded(rule, level);
  const dataAttr = level === 'sub'
    ? `data-portfolio-subrule="${esc(rule.id)}"`
    : `data-portfolio-rule="${esc(rule.id)}"`;
  const childHint = canExpand
    ? `<span class="portfolioRuleDrill">${expanded ? '收起' : `${rule.children.length} 项`}</span>`
    : '';
  const childTotal = childAmountTotal(rule.children);
  const totalDiff = Math.abs(childTotal - n(rule.currentAmountCny));
  const totalCheck = canExpand && childTotal > 0 && totalDiff <= Math.max(1, n(rule.currentAmountCny) * 0.002)
    ? `<span class="portfolioRuleCheck">子项合计 ${esc(rule.currentAmountLabel)}</span>`
    : '';
  return `
    <button type="button" class="portfolioRuleRow ${esc(rule.tone)} level-${esc(level)} ${expanded ? 'is-expanded' : ''}" ${dataAttr} ${canExpand ? `aria-expanded="${expanded ? 'true' : 'false'}"` : ''}>
      <div class="portfolioRuleTop">
        <strong>${esc(rule.name)}</strong>
        <span class="portfolioRuleStatus ${esc(rule.tone)}">${esc(rule.status)}</span>
      </div>
      <div class="portfolioRuleMeta">
        <span>当前 ${fmtPct(rule.current)}</span>
        <span>目标 ${esc(rule.targetLabel)}</span>
        <span>边界 ${esc(rule.limitLabel)}</span>
      </div>
      <div class="portfolioRuleAmounts">
        <span>当前 ${esc(rule.currentAmountLabel)}</span>
        <span>目标 ${esc(rule.targetAmountLabel)}</span>
        <span>边界 ${esc(rule.limitAmountLabel)}</span>
      </div>
      <div class="portfolioRuleFooter">${childHint}${totalCheck}</div>
      ${ruleTrack(rule)}
    </button>
  `;
}

function leafRow(rule, child, index) {
  const subLabel = child.symbol || child.subtitle || (child.name === '其他合计' ? '剩余合计' : '具体标的配置');
  return `
    <div class="portfolioLeafRow ${esc(rule.tone)}">
      <span>${index + 1}</span>
      <div>
        <strong>${esc(child.name || '--')}</strong>
        <small>${esc(subLabel)}</small>
      </div>
      <div>
        <b>${esc(child.amount_label || '--')}</b>
        <em>${esc(child.weight_label || '--')}</em>
      </div>
    </div>
  `;
}

function leafList(rule) {
  if (!rule.children.length) return '';
  return `
    <div class="portfolioLeafList">
      <div class="portfolioHierarchyLabel">
        <span>具体标的</span>
        <em>${rule.children.length} 项</em>
      </div>
      ${rule.children.map((child, index) => leafRow(rule, child, index)).join('')}
    </div>
  `;
}

function linkedChildren(rule) {
  return rule.children.map((child) => ({
    raw: child,
    rule: child.rule_id ? findRule(child.rule_id) : null,
  }));
}

function ruleChildren(rule) {
  if (!rule.children.length || !expandedRuleIds.has(rule.id)) return '';
  const children = linkedChildren(rule);
  const linked = children.filter((child) => child.rule);
  if (!linked.length) {
    return `
      <div class="portfolioRuleChildren leaf-only">
        ${leafList(rule)}
      </div>
    `;
  }
  return `
    <div class="portfolioRuleChildren">
      <div class="portfolioHierarchyLabel">
        <span>二级配置</span>
        <em>${linked.length} 项</em>
      </div>
      ${linked.map(({ rule: childRule }) => `
        <div class="portfolioSubNode" data-rule-node="${esc(childRule.id)}">
          ${ruleRow(childRule, { level: 'sub' })}
          ${expandedSubRuleIds.has(childRule.id) ? leafList(childRule) : ''}
        </div>
      `).join('')}
    </div>
  `;
}

function ruleNode(rule) {
  return `
    <div class="portfolioRuleNode ${esc(rule.tone)}" data-rule-node="${esc(rule.id)}">
      ${ruleRow(rule)}
      ${ruleChildren(rule)}
    </div>
  `;
}

function ruleGroup(group, rules) {
  if (!rules.length) return '';
  return `
    <section class="portfolioRuleGroup" data-rule-group="${esc(group.id)}">
      <div class="portfolioRuleGroupHead">
        <strong>${esc(group.name)}</strong>
        ${group.description ? `<small>${esc(group.description)}</small>` : ''}
      </div>
      <div class="portfolioRuleGroupRows">
        ${rules.map(ruleNode).join('')}
      </div>
    </section>
  `;
}

function renderRuleGroups(rules) {
  const groups = currentGroups.length
    ? currentGroups
    : [...new Set(rules.map((rule) => rule.groupId))].map((id) => ({ id, name: id, description: '' }));
  if (groups.length === 1 && groups[0].id === 'primary_allocation') {
    return rules.filter((rule) => rule.groupId === 'primary_allocation').map(ruleNode).join('');
  }
  return groups.map((group) => {
    const groupRules = rules.filter((rule) => rule.groupId === group.id);
    return ruleGroup(group, groupRules);
  }).join('');
}

function rebalanceRow(item, index) {
  return `
    <button type="button" class="portfolioRebalanceRow ${esc(item.tone || 'info')}" data-rebalance-item="${esc(item.id)}">
      <span>${index + 1}</span>
      <div>
        <strong>${esc(item.title)}</strong>
        <small>${esc(item.text)}</small>
      </div>
      <em class="${esc(item.tone || 'info')}">${esc(item.status)}</em>
    </button>
  `;
}

function renderCurrentPortfolioConfig() {
  const rulesEl = document.getElementById('portfolioConfigRules');
  const queueEl = document.getElementById('portfolioRebalanceQueue');
  const asOfEl = document.getElementById('portfolioConfigAsOf');
  if (asOfEl) asOfEl.textContent = currentAsOf;
  if (rulesEl) rulesEl.innerHTML = renderRuleGroups(currentRules);
  if (queueEl) {
    if (!currentRebalance.length) {
      queueEl.innerHTML = dataStateHtml({
        tone: 'empty',
        title: '暂无再平衡动作',
        detail: '当前目标配置没有触发需要执行的再平衡建议。',
        compact: true,
      });
      return;
    }
    queueEl.innerHTML = currentRebalance.map(rebalanceRow).join('')
      || '<div class="lookthroughEmpty compact">当前没有再平衡动作</div>';
  }
}

function renderPortfolioConfig(payload) {
  currentRules = (payload?.rules || []).map(normalizeRule);
  currentDrilldownRules = (payload?.drilldown_rules || []).map(normalizeRule);
  currentGroups = (payload?.groups || []).map(normalizeGroup);
  currentRebalance = payload?.rebalance || [];
  currentAsOf = payload?.as_of || '--';
  renderCurrentPortfolioConfig();
}

function bindPortfolioConfigEvents() {
  document.addEventListener('click', (event) => {
    const subRuleButton = event.target?.closest?.('[data-portfolio-subrule]');
    if (subRuleButton) {
      const id = subRuleButton.dataset.portfolioSubrule;
      if (expandedSubRuleIds.has(id)) expandedSubRuleIds.delete(id);
      else expandedSubRuleIds.add(id);
      renderCurrentPortfolioConfig();
      return;
    }
    const ruleButton = event.target?.closest?.('[data-portfolio-rule]');
    if (ruleButton) {
      const id = ruleButton.dataset.portfolioRule;
      if (expandedRuleIds.has(id)) expandedRuleIds.delete(id);
      else expandedRuleIds.add(id);
      renderCurrentPortfolioConfig();
      return;
    }
    const actionButton = event.target?.closest?.('[data-rebalance-item]');
    if (actionButton) {
      const item = currentRebalance.find((row) => row.id === actionButton.dataset.rebalanceItem);
      if (item) {
        openBottomSheet(item.title, `<div class="portfolioConfigSheet"><p>${esc(item.text)}</p></div>`);
      }
    }
  });
}

async function loadPortfolioConfig() {
  if (!document.getElementById('portfolio-constraints')) return;
  try {
    const payload = await jget('/api/portfolio/config?baseCurrency=CNY', { cache: false });
    renderPortfolioConfig(payload);
  } catch (error) {
    const rulesEl = document.getElementById('portfolioConfigRules');
    if (rulesEl) {
      rulesEl.innerHTML = dataStateHtml({
        tone: 'error',
        title: '组合配置读取失败',
        detail: error?.message || error,
      });
      return;
    }
    if (rulesEl) rulesEl.innerHTML = `<div class="lookthroughEmpty">组合配置读取失败：${esc(error?.message || error)}</div>`;
  }
}

bindPortfolioConfigEvents();
loadPortfolioConfig();
