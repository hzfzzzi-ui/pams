// Explicit fixed synthetic data for presentation/lifecycle contracts only.
export const options = {
  accounts: [{account_code: 'fixture-account', display_name: '合成账户'}], categories: [], sources: ['fact'],
  assets: [{asset_key: 'fixture-a', symbol: 'FIXA', display_name: '合成资产 A', trade_count: 12, pending_count: 2}],
  review_options: {
    buy_reasons: [{value: 'research', label: '合成研究原因'}, {value: 'uncertain', label: '未确定 / 待复盘'}],
    sell_reasons: [{value: 'reduce', label: '合成减仓原因'}],
    position_identities: [{value: 'allocation', label: '配置仓'}, {value: 'value', label: '价值仓'}, {value: 'uncertain', label: '未确定 / 待判断'}, {value: 'closed', label: '已关闭'}],
    current_position_identities: [{value: 'allocation', label: '配置仓'}, {value: 'value', label: '价值仓'}, {value: 'uncertain', label: '未确定 / 待判断'}, {value: 'closed', label: '已关闭'}],
    sell_rule_types: [{value: 'allocation_rebalance', label: '合成再平衡规则'}, {value: 'value_thesis', label: '合成价值规则'}],
    identity_confidences: [{value: 'high', label: '高'}],
    transition_types: [{value: 'research_update', label: '合成研究更新'}],
    transition_triggers: [{value: 'new_evidence', label: '合成新证据'}],
    review_tags: [{value: 'disciplined', label: '合成纪律', group: 'behavior'}, {value: 'research_complete', label: '合成研究完成', group: 'post_review'}],
  },
};
export const rows = Array.from({length: 12}, (_, index) => ({
  row_id: `fact-${index+1}`, native_id: index+1, source: 'fact', account_code: 'fixture-account', account_name: '合成账户',
  trade_date: `2026-10-${String(30-index).padStart(2, '0')}`, major_category: 'A股', minor_category: '股票', symbol: `FIX${index+1}`, name: `合成资产 ${index+1}`,
  side_normalized: index % 2 ? 'sell' : 'buy', side: index % 2 ? 'sell' : 'buy', quantity: 2, price: 25, gross_amount: 50, fees: 0.5, currency: 'CNY', status: 'auto',
  owner_ratio: 0.5, gross_amount_personal_cny: 25, fees_personal_cny: 0.25,
  buy_reason: index % 2 ? '' : 'research', sell_reason: index % 2 ? 'reduce' : '', review_tags: [],
  initial_position_identity: 'allocation', current_position_identity: 'allocation', intended_holding_period: '长期', max_position_pct: '20', sell_rule_type: 'allocation_rebalance', sell_condition: '合成原始退出条件',
}));
export const ledger = {rows, summary: {count: 12, buy_amount_cny: 300, sell_amount_cny: 300, fees_cny: 6, buy_amount_personal_cny: 150, sell_amount_personal_cny: 150, fees_personal_cny: 3, has_owner_adjustments: true, sources: ['fact']}};
export const attribution = {
  sample_count: 12, missing_price_count: 0, groups: {buy_reason: [], sell_reason: []}, data_quality: {note: '固定测试数据'}, workbench: {},
  asset_heatmap: {positive_action_count: 1, negative_action_count: 1, assets: [
    {key: 'fixture-a', name: '合成资产 A', display_name: '合成资产 A', symbol: 'FIXA', actual_total_pnl_cny: 50, estimated_decision_effect_cny: 20, trade_count: 2, positive_count: 1, negative_count: 1, trades: [
      {trade_id: 1, side: 'buy', trade_date: '2026-10-30', estimated_decision_effect_cny: 20, name: '合成资产 A', label: '合成买入贡献', reason: '合成证据'},
      {trade_id: 2, side: 'sell', trade_date: '2026-10-29', estimated_decision_effect_cny: -10, name: '合成资产 A', label: '合成卖出贡献', reason: '合成证据'},
    ]},
    {key: 'fixture-b', name: '合成资产 B', symbol: 'FIXB', actual_total_pnl_cny: -25, estimated_decision_effect_cny: -10, trade_count: 1, negative_count: 1, trades: []},
  ]},
};
export const tradeAttribution = (id, label = `合成结论 ${id}`) => ({
  trade_id: id, side: 'buy', trade: {trade_id: id, trade_type: 'buy', account_code: 'fixture-account', trade_date: '2026-10-30', symbol: `FIX${id}`, name: `合成资产 ${id}`, gross_amount: 50, fee_amount: 0.4, tax_amount: 0.1, currency: 'CNY', buy_reason: 'research', initial_position_identity: 'allocation', current_position_identity: 'allocation'},
  event_metrics: {coverage: 'missing', warnings: ['合成价格缺口']}, auto_review: {label, summary: `${label} 说明`, confidence: 'high', level: 'muted', action: '合成人工检查', suggested_tags: []},
});
export const campaigns = {summary: {total_campaigns: 0, status_counts: {}}, campaigns: [], data_quality: {note: '固定空周期'}};
