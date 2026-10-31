import { createReadScope, isReadCancelled } from '../../shared/read_scope.js?v=20261031-modules-v1';
import { accountLabel } from '../../api.js?v=20261031-modules-v1';
import { escapeHtml, formatPlainMoney as money } from '../../utils.js?v=20261031-modules-v1';

export function createEditor({state, scope, legacy, attribution, get, reads = createReadScope({get})}) {
const $ = scope.byId;
const openBottomSheet = scope.openSheet;
let openingRequest = 0;
scope.onSheetClose?.(() => {
  reads.cancel('editor-open');
  reads.cancel('editor-attribution');
});
const {COMMON_REVIEW_TAG_VALUES, HOLDING_PERIOD_OPTIONS, REVIEW_TAG_GROUP_LABELS, REVIEW_TAG_LABEL_OVERRIDES, addDaysIso, applyIdentityDefaults, findTradeRow, holdingDisplay, identityRule, option, reviewOptionLabel, reviewOptionList, reviewTextValue, ruleLabel, sideLabel, tradeRowFromAttributionPayload} = legacy;
const {TradeAutoAttributionPanelV3, renderTradeAutoAttribution} = attribution;

function renderReviewChoiceOptions(key, selectedValue, inputName, opts = {}) {
  const selected = String(selectedValue || '');
  const items = reviewOptionList(key)
    .filter((item) => !opts.excludeValues?.includes?.(item.value));
  return `
    ${items.map((item) => {
      const checked = item.value === selected;
      return `
        <label class="tradeReviewTag ${checked ? 'checked' : ''}">
          <input type="radio" name="${escapeHtml(inputName)}" value="${escapeHtml(item.value)}"${checked ? ' checked' : ''}>
          <span>${escapeHtml(item.label)}</span>
        </label>
      `;
    }).join('')}
  `;
}

function renderStaticChoiceOptions(items, selectedValue, inputName) {
  const selected = String(selectedValue || '');
  return (items || []).map((item) => {
    const checked = item.value === selected;
    return `
      <label class="tradeReviewTag ${checked ? 'checked' : ''}">
        <input type="radio" name="${escapeHtml(inputName)}" value="${escapeHtml(item.value)}"${checked ? ' checked' : ''}>
        <span>${escapeHtml(item.label)}</span>
      </label>
    `;
  }).join('');
}

function renderOptionChoices(key, selectedValue) {
  const selected = String(selectedValue || '');
  return [
    '<option value="">不选择</option>',
    ...reviewOptionList(key).map((item) => option(item.value, item.label, item.value === selected)),
  ].join('');
}

function renderTransitionTriggerOptions(selectedTriggers) {
  const selected = new Set(Array.isArray(selectedTriggers) ? selectedTriggers : []);
  return reviewOptionList('transition_triggers').map((item) => `
    <label class="tradeReviewTag ${selected.has(item.value) ? 'checked' : ''}">
      <input type="checkbox" name="identityTransitionTrigger" value="${escapeHtml(item.value)}"${selected.has(item.value) ? ' checked' : ''}>
      <span>${escapeHtml(item.label)}</span>
    </label>
  `).join('');
}

function reviewTagDisplayLabel(item) {
  return REVIEW_TAG_LABEL_OVERRIDES[item?.value] || item?.label || item?.value || '';
}

function renderReviewTagGroup(label, items, selected) {
  if (!items.length) return '';
  return `
    <div class="tradeReviewTagGroup">
      <div class="tradeReviewGroupTitle">${escapeHtml(label)}</div>
      <div class="tradeReviewTagGrid">
        ${items.map((item) => `
          <label class="tradeReviewTag ${selected.has(item.value) ? 'checked' : ''}">
            <input type="checkbox" name="reviewTag" data-tag-group="${escapeHtml(item.group || 'post_review')}" value="${escapeHtml(item.value)}"${selected.has(item.value) ? ' checked' : ''}>
            <span>${escapeHtml(reviewTagDisplayLabel(item))}</span>
          </label>
        `).join('')}
      </div>
    </div>
  `;
}

function renderReviewTagOptions(selectedTags) {
  const selected = new Set(Array.isArray(selectedTags) ? selectedTags : []);
  const grouped = reviewOptionList('review_tags').reduce((acc, item) => {
    const group = item.group || 'post_review';
    if (!acc[group]) acc[group] = [];
    acc[group].push(item);
    return acc;
  }, {});
  const common = Object.entries(REVIEW_TAG_GROUP_LABELS).map(([group, label]) => {
    const items = (grouped[group] || []).filter((item) => COMMON_REVIEW_TAG_VALUES.has(item.value));
    return renderReviewTagGroup(label, items, selected);
  }).join('');
  const more = Object.entries(REVIEW_TAG_GROUP_LABELS).map(([group, label]) => {
    const items = (grouped[group] || []).filter((item) => !COMMON_REVIEW_TAG_VALUES.has(item.value));
    return renderReviewTagGroup(label, items, selected);
  }).join('');
  return `
    ${common}
    ${more.trim() ? `
      <details class="tradeReviewMoreTags">
        <summary>更多标签</summary>
        ${more}
      </details>
    ` : ''}
  `;
}

async function openTradeReviewById(tradeId) {
  if (!scope.alive) return;
  const request = ++openingRequest;
  reads.cancel('editor-open');
  const sheetVersion = scope.sheetVersion;
  const existing = findTradeRow(tradeId);
  if (existing) {
    openTradeReviewSheet(existing);
    return;
  }
  let payload;
  try {
    payload = await reads.get('editor-open', `/api/investment-transactions/${encodeURIComponent(tradeId)}/attribution`, { cache: false });
  } catch (error) {
    if (scope.alive && request === openingRequest && sheetVersion === scope.sheetVersion) throw error;
    return;
  }
  if (!scope.alive || request !== openingRequest || sheetVersion !== scope.sheetVersion) return;
  const row = tradeRowFromAttributionPayload(payload);
  if (!row) throw new Error(`trade not found: ${tradeId}`);
  openTradeReviewSheet(row);
}

async function loadTradeAttribution(tradeId, form = $('tradeReviewForm')) {
  const payload = await reads.get('editor-attribution', `/api/investment-transactions/${encodeURIComponent(tradeId)}/attribution`, { cache: false });
  if (!scope.isCurrentForm(form) || String(form.dataset.tradeId) !== String(tradeId)) return;
  renderTradeAutoAttribution(payload);
}

function openTradeReviewSheet(row) {
  if (!scope.alive) return;
  openingRequest++;
  reads.cancel('editor-open');
  reads.cancel('editor-attribution');
  const holding = holdingDisplay(row);
  const side = String(row?.side_normalized || '').toLowerCase();
  const showBuyReason = side === 'buy' || row?.buy_reason;
  const showSellReason = side === 'sell' || row?.sell_reason;
  const account = row.account_name || accountLabel(row.account_code);
  const initialIdentity = row.initial_position_identity || '';
  const currentIdentity = row.current_position_identity || initialIdentity || '';
  const effectiveIdentity = currentIdentity || initialIdentity;
  const rule = identityRule(effectiveIdentity);
  const holdingPeriod = row.intended_holding_period || rule.holdingPeriod || '';
  const sellRuleType = row.sell_rule_type || rule.sellRuleType || '';
  const sellCondition = row.sell_condition || rule.sellCondition || '';
  const maxPositionPct = row.max_position_pct || rule.maxPositionPct || '';
  const nextReviewDate = row.next_review_date || row.review_due_date || (effectiveIdentity === 'uncertain' ? addDaysIso(14) : '');
  const transitionVisible = Boolean(initialIdentity && currentIdentity && initialIdentity !== currentIdentity);
  const title = `交易复盘 · ${holding.name}`;
  openBottomSheet(title, `
    <form id="tradeReviewForm" class="tradeReviewForm" data-trade-id="${escapeHtml(String(row.native_id))}" data-side="${escapeHtml(side)}">
      <div class="tradeReviewContext">
        <div>
          <span>交易</span>
          <strong>${escapeHtml(sideLabel(side))} · ${escapeHtml(money(row.gross_amount, row.currency))}</strong>
        </div>
        <div>
          <span>账户</span>
          <strong>${escapeHtml(account || '--')}</strong>
        </div>
        <div>
          <span>日期</span>
          <strong>${escapeHtml(row.trade_date || '--')}</strong>
        </div>
      </div>

      <div class="tradeReviewModeHero">
        <div>
          <span>快速记录</span>
          <strong>30 秒记录交易目的，复杂判断之后再补。</strong>
        </div>
        <small>高级计划、事后标签、身份变化默认折叠。</small>
      </div>

      <div class="tradeReviewSubsection tradeReviewQuickSection">
        <div class="tradeReviewSubsectionHead">
          <strong>主原因</strong>
          <small>单选，不默认未标注；不确定时选“未确定 / 待复盘”。</small>
        </div>
        <div class="tradeReviewFieldGrid">
          <div class="tradeReviewField" ${showBuyReason ? '' : 'hidden'}>
            <span>买入原因</span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderReviewChoiceOptions('buy_reasons', row.buy_reason, 'reviewBuyReason')}</div>
          </div>
          <div class="tradeReviewField" ${showSellReason ? '' : 'hidden'}>
            <span>卖出原因</span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderReviewChoiceOptions('sell_reasons', row.sell_reason, 'reviewSellReason')}</div>
          </div>
        </div>

        <div class="tradeReviewFieldGrid">
          <div class="tradeReviewField">
            <span>初始仓位身份</span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderReviewChoiceOptions('position_identities', initialIdentity, 'reviewInitialIdentity')}</div>
          </div>
          <div class="tradeReviewField">
            <span>当前仓位身份 <button id="btnShowIdentityTransition" class="tradeReviewInlineBtn" type="button">修改身份</button></span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderReviewChoiceOptions('current_position_identities', currentIdentity, 'reviewCurrentIdentity')}</div>
          </div>
        </div>
        <div id="identityRuleHint" class="tradeReviewRuleHint">${escapeHtml(rule.hint || '选择仓位身份后自动生成卖出规则和仓位上限。')}</div>
        <div id="uncertainPositionGuard" class="tradeReviewWarning" ${effectiveIdentity === 'uncertain' ? '' : 'hidden'}>
          不确定可以，但必须小仓位、限期复盘、不能无限加仓。系统默认仓位上限 1%–2%，并要求设置复盘日期。
        </div>
        <div class="tradeReviewFieldGrid tradeReviewCompactGrid">
          <div class="tradeReviewField">
            <span>预计持有周期</span>
            <div class="tradeReviewTagGrid tradeReviewChoiceGrid">${renderStaticChoiceOptions(HOLDING_PERIOD_OPTIONS, holdingPeriod, 'reviewHoldingPeriod')}</div>
          </div>
          <label class="tradeReviewField">
            <span>最大仓位上限 %</span>
            <input id="reviewMaxPositionPct" class="formInput mono" type="number" min="0" step="0.01" value="${escapeHtml(maxPositionPct)}" data-auto-generated="${row.max_position_pct ? '0' : '1'}" placeholder="按仓位身份建议" />
          </label>
          <div class="tradeReviewField tradeReviewRuleCard">
            <span>适用卖出规则</span>
            <input id="reviewSellRuleType" type="hidden" value="${escapeHtml(sellRuleType)}" data-auto-generated="${row.sell_rule_type ? '0' : '1'}" />
            <strong id="reviewSellRuleText">${escapeHtml(ruleLabel(sellRuleType))}</strong>
            <small id="reviewSellRuleDetail">${escapeHtml(sellCondition || '选择仓位身份后自动生成。')}</small>
          </div>
        </div>
        <label class="tradeReviewField">
          <span>一句备注（可选）</span>
          <textarea id="reviewNote" class="formInput tradeReviewTextarea compact" rows="2" placeholder="一句话记录背景、想法或主观判断。">${reviewTextValue(row.review_note)}</textarea>
        </label>
      </div>

      <details class="tradeReviewFold tradeReviewAutoFold" id="reviewAutoAttributionDetails" open>
        <summary>
          <strong>查看交易后表现</strong>
          <small>自动归因</small>
        </summary>
        ${TradeAutoAttributionPanelV3(null)}
      </details>

      <details class="tradeReviewFold" id="reviewPlanDetails">
        <summary>
          <strong>展开计划细节</strong>
          <small id="reviewPlanCompletion">计划细节 0/5</small>
        </summary>
        <div class="tradeReviewFieldGrid tradeReviewCompactGrid">
          <label class="tradeReviewField">
            <span>最大可接受亏损 %</span>
            <input id="reviewMaxLossPct" class="formInput mono" type="number" min="0" step="0.01" value="${escapeHtml(row.max_acceptable_loss_pct || '')}" />
          </label>
          <label class="tradeReviewField">
            <span>下次复盘日期</span>
            <input id="reviewNextReviewDate" class="formInput" type="date" value="${escapeHtml(nextReviewDate)}" data-auto-generated="${(row.next_review_date || row.review_due_date) ? '0' : '1'}" />
          </label>
          <label class="tradeReviewField">
            <span>身份置信度</span>
            <select id="reviewIdentityConfidence" class="formInput">${renderOptionChoices('identity_confidences', row.identity_confidence)}</select>
          </label>
        </div>
        <label class="tradeReviewField">
          <span>买入逻辑 / 不确定原因</span>
          <textarea id="reviewBuyLogic" class="formInput tradeReviewTextarea" rows="2" placeholder="记录为什么买、为什么不确定、需要补充什么研究。">${reviewTextValue(row.buy_logic)}</textarea>
        </label>
        <label class="tradeReviewField">
          <span>加仓条件</span>
          <textarea id="reviewAddCondition" class="formInput tradeReviewTextarea" rows="2" placeholder="没有重新分类前，试验仓/未确定仓不应继续加仓。">${reviewTextValue(row.add_condition)}</textarea>
        </label>
        <label class="tradeReviewField">
          <span>减仓条件</span>
          <textarea id="reviewReduceCondition" class="formInput tradeReviewTextarea" rows="2">${reviewTextValue(row.reduce_condition)}</textarea>
        </label>
        <label class="tradeReviewField">
          <span>卖出 / 证伪条件</span>
          <textarea id="reviewSellCondition" class="formInput tradeReviewTextarea" rows="2" data-auto-generated="${row.sell_condition ? '0' : '1'}" placeholder="趋势破坏、逻辑证伪、事件失败、目标比例偏离等。">${reviewTextValue(sellCondition)}</textarea>
        </label>
      </details>

      <details class="tradeReviewFold" id="reviewTagDetails">
        <summary>
          <strong>填写事后复盘</strong>
          <small id="reviewTagCompletion">复盘标签 0/3</small>
        </summary>
        <div class="tradeReviewField">
          <span>复盘标签</span>
          <div class="tradeReviewTagGrid">${renderReviewTagOptions(row.review_tags)}</div>
        </div>
      </details>

      <div id="identityTransitionSection" class="tradeReviewSubsection tradeReviewTransitionSection" ${transitionVisible ? '' : 'hidden'}>
        <div class="tradeReviewSubsectionHead">
          <strong>身份变化留痕</strong>
          <small>仅当身份变化或主动修改身份时填写。</small>
        </div>
        <div class="tradeReviewFieldGrid tradeReviewCompactGrid">
          <div class="tradeReviewReadonlyField">
            <span>原身份</span>
            <strong id="transitionOldIdentityText">${escapeHtml(reviewOptionLabel('position_identities', initialIdentity) || '--')}</strong>
          </div>
          <div class="tradeReviewReadonlyField">
            <span>新身份</span>
            <strong id="transitionNewIdentityText">${escapeHtml(reviewOptionLabel('current_position_identities', currentIdentity) || '--')}</strong>
          </div>
          <div class="tradeReviewReadonlyField">
            <span>新卖出规则</span>
            <strong id="transitionNewSellRuleText">${escapeHtml(ruleLabel(sellRuleType))}</strong>
          </div>
          <div class="tradeReviewReadonlyField">
            <span>新仓位上限 / 复盘日</span>
            <strong><em id="transitionNewLimitText">${escapeHtml(currentIdentity === 'closed' ? '不适用' : (maxPositionPct ? `${maxPositionPct}%` : '--'))}</em> · <em id="transitionReviewDateText">${escapeHtml(currentIdentity === 'closed' ? '不适用' : (nextReviewDate || '--'))}</em></strong>
          </div>
        </div>
        <div id="trendToValueWarning" class="tradeReviewWarning danger" ${initialIdentity === 'trend' && currentIdentity === 'value' ? '' : 'hidden'}>
          这是高风险身份变化。请确认不是因为亏损后不愿止损；建议补充基本面证据、估值区间、新卖出条件和新仓位上限，也可以先保存后再完善。
        </div>
        <label class="tradeReviewField">
          <span>变化类型</span>
          <select id="reviewTransitionType" class="formInput">${renderOptionChoices('transition_types', '')}</select>
        </label>
        <div class="tradeReviewField">
          <span>触发原因</span>
          <div class="tradeReviewTagGrid">${renderTransitionTriggerOptions([])}</div>
        </div>
        <label class="tradeReviewField">
          <span>变更证据</span>
          <textarea id="reviewTransitionEvidence" class="formInput tradeReviewTextarea" rows="2" placeholder="补充研究、新卖出规则、新仓位上限、估值区间或数据修正说明。"></textarea>
        </label>
      </div>

      <div id="tradeReviewMsg" class="tradeReviewMsg muted"></div>
      <div class="tradeReviewActionBar">
        <span id="tradeReviewQualityBadge" class="tradeReviewQualityBadge warn">信息不足</span>
        <button type="submit" class="btn primary tradeReviewSaveBtn">保存复盘</button>
      </div>
    </form>
  `);
  const form = $('tradeReviewForm');
  applyIdentityDefaults(form);
  loadTradeAttribution(row.native_id, form).catch(error => {
    if (isReadCancelled(error) || !scope.isCurrentForm(form)) return;
    renderTradeAutoAttribution({
    event_metrics: { coverage: 'missing', warnings: ['自动表现暂时不可用。'] },
    judgements: [{ label: '不可用', message: '自动归因接口暂时不可用。', level: 'muted' }],
    });
  });
}

return {renderReviewChoiceOptions, renderStaticChoiceOptions, renderOptionChoices, renderTransitionTriggerOptions, reviewTagDisplayLabel, renderReviewTagGroup, renderReviewTagOptions, openTradeReviewById, loadTradeAttribution, openTradeReviewSheet};
}
