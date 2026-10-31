import { readOptionalNumber as readNumber } from '../../utils.js?v=20261031-gray-v1';

export function createCommands({scope, legacy, post, loadOptions, loadLedger}) {
const $ = scope.byId;
const readOptionalNumber = id => readNumber($(id));
const {validateTradeReviewForm, selectedCheckboxValues, radioInputValue, optionalInputValue} = legacy;
const saving = new WeakSet();

async function saveTradeReview(form) {
  if (!scope.isCurrentForm(form) || saving.has(form)) return;
  const tradeId = form?.dataset?.tradeId;
  if (!tradeId) return;
  const msg = $('tradeReviewMsg');
  const validationError = validateTradeReviewForm(form);
  if (validationError) {
    if (msg) msg.textContent = validationError;
    return;
  }
  const selectedTags = selectedCheckboxValues(form, 'reviewTag');
  const transitionTriggers = selectedCheckboxValues(form, 'identityTransitionTrigger');
  const payload = {
    buy_reason: radioInputValue(form, 'reviewBuyReason'),
    sell_reason: radioInputValue(form, 'reviewSellReason'),
    review_note: optionalInputValue('reviewNote'),
    review_tags: selectedTags,
    initial_position_identity: radioInputValue(form, 'reviewInitialIdentity'),
    current_position_identity: radioInputValue(form, 'reviewCurrentIdentity'),
    identity_confidence: optionalInputValue('reviewIdentityConfidence'),
    intended_holding_period: radioInputValue(form, 'reviewHoldingPeriod'),
    buy_logic: optionalInputValue('reviewBuyLogic'),
    add_condition: optionalInputValue('reviewAddCondition'),
    reduce_condition: optionalInputValue('reviewReduceCondition'),
    sell_condition: optionalInputValue('reviewSellCondition'),
    max_acceptable_loss_pct: readOptionalNumber('reviewMaxLossPct'),
    max_position_pct: readOptionalNumber('reviewMaxPositionPct'),
    next_review_date: optionalInputValue('reviewNextReviewDate'),
    sell_rule_type: optionalInputValue('reviewSellRuleType'),
    identity_transition_type: optionalInputValue('reviewTransitionType'),
    identity_transition_triggers: transitionTriggers,
    identity_transition_evidence: optionalInputValue('reviewTransitionEvidence'),
  };
  if (msg) msg.textContent = '正在保存...';
  saving.add(form);
  const button = form.querySelector('button[type="submit"]');
  const wasDisabled = button?.disabled;
  if (button) button.disabled = true;
  let completed = false;
  try {
    await post(`/api/investment-transactions/${encodeURIComponent(tradeId)}/review`, payload);
    if (!scope.isCurrentForm(form)) return;
    completed = true;
    if (msg) msg.textContent = '已保存';
    scope.closeSheet();
    await loadOptions();
    if (scope.alive) await loadLedger();
  } catch (error) {
    if (scope.alive && (completed || scope.isCurrentForm(form))) throw error;
  } finally {
    saving.delete(form);
    if (button && scope.isCurrentForm(form)) button.disabled = wasDisabled;
  }
}

return {saveTradeReview};
}
