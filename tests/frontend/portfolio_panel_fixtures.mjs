// Explicit synthetic DTOs; never copied from an account or production response.
export const riskPayload = {
  as_of: '2026-01-02',
  dashboard: {score: 28, max_score: 100, score_label: '28', status_tone: 'warn', status_label: '固定测试观察',
    category_scores: [{tone: 'warn'}, {tone: 'ok'}]},
  categories: [
    {id: 'liquidity', name: '固定流动性', description: '合成风险项', tone: 'warn', headline: '测试摘要',
      checklist: {score: 25, status_label: '待检查', counts: {ok: 1, info: 0, warn: 1, risk: 0}, total: 2},
      items: [{name: '固定风险项', tone: 'warn', weight_label: '40.0%', thresholds: {warn: 0.3, risk: 0.5},
        rule: {formula: '测试比例', adjustment_direction: '现金缓冲'}, sources: [{name: 'FIX - 合成资产', weight_label: '40.0%'}]}]},
    {id: 'concentration', name: '固定集中度', tone: 'ok', items: [{name: '固定正常项', tone: 'ok', weight_label: '10.0%'}]},
  ],
};

export const configPayload = {
  as_of: '2026-01-02',
  groups: [{id: 'primary_allocation', name: '固定主配置'}],
  rules: [{id: 'equity', group_id: 'primary_allocation', name: '固定权益', current: 0.4,
    target_low: 0.3, target_high: 0.5, hard_high: 0.6, tone: 'ok', status: '目标内', target_label: '30%–50%', limit_label: '≤60%',
    current_amount_cny: 1000, current_amount_label: '¥1,000.00', target_amount_label: '固定目标', limit_amount_label: '固定边界',
    children: [{rule_id: 'domestic', name: '固定子配置', amount_cny: 1000}]}],
  drilldown_rules: [{id: 'domestic', group_id: 'primary_allocation', name: '固定子配置', current: 0.25,
    target_low: null, target_high: 0.4, hard_high: null, tone: 'warn', status: '观察', target_label: '≤40%', limit_label: '--',
    current_amount_cny: 1000, current_amount_label: '¥1,000.00', target_amount_label: '--', limit_amount_label: '--',
    children: [{name: '固定叶节点', symbol: 'FIXTURE', amount_cny: 1000, amount_label: '¥1,000.00', weight_label: '25.0%'}]}],
  rebalance: [{id: 'rebalance-fixture', title: '固定再平衡详情', text: '说明 <保留> & 原文', tone: 'info', status: '观察'}],
};
