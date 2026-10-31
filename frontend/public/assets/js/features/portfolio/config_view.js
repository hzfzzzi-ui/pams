import {dataStateHtml} from '../../components/data_state.js?v=20261031-modules-v1';

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

function findRule(id, state) {
  return [...state.currentRules, ...state.currentDrilldownRules].find((item) => item.id === id);
}

function ruleExpanded(rule, level, state) {
  return level === 'sub' ? state.expandedSubRuleIds.has(rule.id) : state.expandedRuleIds.has(rule.id);
}

function ruleRow(rule, state, { level = 'top' } = {}) {
  const canExpand = rule.children.length > 0;
  const expanded = ruleExpanded(rule, level, state);
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

function linkedChildren(rule, state) {
  return rule.children.map((child) => ({
    raw: child,
    rule: child.rule_id ? findRule(child.rule_id, state) : null,
  }));
}

function ruleChildren(rule, state) {
  if (!rule.children.length || !state.expandedRuleIds.has(rule.id)) return '';
  const children = linkedChildren(rule, state);
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
          ${ruleRow(childRule, state, { level: 'sub' })}
          ${state.expandedSubRuleIds.has(childRule.id) ? leafList(childRule) : ''}
        </div>
      `).join('')}
    </div>
  `;
}

function ruleNode(rule, state) {
  return `
    <div class="portfolioRuleNode ${esc(rule.tone)}" data-rule-node="${esc(rule.id)}">
      ${ruleRow(rule, state)}
      ${ruleChildren(rule, state)}
    </div>
  `;
}

function ruleGroup(group, rules, state) {
  if (!rules.length) return '';
  return `
    <section class="portfolioRuleGroup" data-rule-group="${esc(group.id)}">
      <div class="portfolioRuleGroupHead">
        <strong>${esc(group.name)}</strong>
        ${group.description ? `<small>${esc(group.description)}</small>` : ''}
      </div>
      <div class="portfolioRuleGroupRows">
        ${rules.map(rule => ruleNode(rule, state)).join('')}
      </div>
    </section>
  `;
}

function renderRuleGroups(rules, state) {
  const groups = state.currentGroups.length
    ? state.currentGroups
    : [...new Set(rules.map((rule) => rule.groupId))].map((id) => ({ id, name: id, description: '' }));
  if (groups.length === 1 && groups[0].id === 'primary_allocation') {
    return rules.filter((rule) => rule.groupId === 'primary_allocation').map(rule => ruleNode(rule, state)).join('');
  }
  return groups.map((group) => {
    const groupRules = rules.filter((rule) => rule.groupId === group.id);
    return ruleGroup(group, groupRules, state);
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

export function configModelFromPayload(payload) {
  return {
    currentRules: (payload?.rules || []).map(normalizeRule),
    currentDrilldownRules: (payload?.drilldown_rules || []).map(normalizeRule),
    currentGroups: (payload?.groups || []).map(normalizeGroup),
    currentRebalance: payload?.rebalance || [],
    currentAsOf: payload?.as_of || '--',
  };
}

export function rebalanceHtml(currentRebalance) {
  if (!currentRebalance.length) return dataStateHtml({
    tone: 'empty', title: '暂无再平衡动作',
    detail: '当前目标配置没有触发需要执行的再平衡建议。', compact: true,
  });
  return currentRebalance.map(rebalanceRow).join('') || '<div class="lookthroughEmpty compact">当前没有再平衡动作</div>';
}

export function configErrorHtml(error) {
  return dataStateHtml({tone: 'error', title: '组合配置读取失败', detail: error?.message || error});
}

export function rebalanceDetailHtml(item) {
  return `<div class="portfolioConfigSheet"><p>${esc(item.text)}</p></div>`;
}

export {renderRuleGroups};
