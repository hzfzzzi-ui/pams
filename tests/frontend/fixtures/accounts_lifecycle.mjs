// Explicit synthetic records; no account, ledger original or production value.
export const config = {
  defaults: {since: '2026-01-01'},
  profiles: {
    synthetic_cny: {label: '合成银行模板', currency: 'CNY', bank: '合成银行', account_kind: 'bank', account_code: 'fixture/cny', statement_template: '合成列映射', pams_target: {account_code: 'fixture/cny'}},
    synthetic_hkd: {label: '合成港币模板', currency: 'HKD', bank: '合成平台', account_kind: 'wallet', account_code: 'fixture/hkd', since: '2026-02-02', statement_template: '合成港币列'},
  },
  account_entities: {bank: {label: '合成银行', role: '合成资金来源'}, broker: {label: '合成券商', role: '合成投资账户'}},
  account_relationships: [{from_account: 'bank', to_account: 'broker', relation_type: 'investment_funding', label: '合成转账关系', code: 'synthetic-relation'}],
};
export const overview = {
  summary: {ready_for_review: 1, missing_data: 1, stale_review: 1, in_transit: 0},
  accounts: [
    {account_code: 'fixture/cny', display_name: '合成 <人民币>', account_root_code: 'bank', account_root_label: '合成银行', account_root_order: 1, account_nature_label: '银行账户', base_currency: 'CNY', status: 'ready_for_review', status_label: '待核准', balance_basis_label: '合成账单', review_hint: '核对合成金额', source: 'synthetic', statement_balance: {amount: 1000}, platform_balance: {amount: 980}, primary_balance: {amount: 1000, currency: 'CNY'}, review_status: 'stale', review_stale_reason: '合成日期过期'},
    {account_code: 'fixture/hkd', display_name: '合成港币', account_root_code: 'wallet', account_root_label: '合成平台', account_root_order: 2, base_currency: 'HKD', status: 'missing_manual', status_label: '缺少手动余额', manual_balance: {amount: null, note: '合成备注'}, platform_balances: [{currency: 'HKD', market_value: 88.25}], account_type: 'asset'},
    {account_code: 'fixture/card', display_name: '合成信用卡', account_root_code: 'card', account_root_label: '合成卡', account_root_order: 3, base_currency: 'USD', account_type: 'liability', account_nature: 'credit_card', status: 'approved', status_label: '已确认', primary_balance: {amount: -50, currency: 'USD'}, statement_balance: {amount: -50}, latest_review: {reviewed_at: '2026-10-29'}},
    {account_code: 'fixture/owner', display_name: '合成个人份额', account_root_code: 'bank', account_root_label: '合成银行', base_currency: 'CNY', status: 'approved', status_label: '已确认', primary_balance: {amount: 3000, currency: 'CNY', gross_amount: 5000, owner_ratio: 0.6}},
  ],
};
export const matches = {
  matched: 1,
  summary: [{match_type: 'account_transfer', match_count: 1, total_amount: 88.25, currency: 'HKD'}],
  matches: [{match_date: '2026-10-29', match_type: 'account_transfer', from_account_code: 'fixture/cny', to_account_code: 'fixture/hkd', amount: 88.25, currency: 'HKD', confidence: 0.95, note: '合成匹配说明', source_date: '2026-10-28', source_summary: '合成转出', source_description: '合成来源备注', target_row_id: 'target-fixture', target_date: '2026-10-29', target_summary: '合成转入', target_description: '合成去向备注'}],
};
export const unmatched = {
  summary: [{issue_type: 'one_sided_match', count: 1, account_code: 'fixture/hkd', amount_abs: 12.5, currency: 'HKD'}],
  rows: [{transaction_date: '2026-10-30', issue_type: 'one_sided_match', account_code: 'fixture/hkd', amount: -12.5, currency: 'HKD', category: '合成分类', subcategory: '合成子类', summary: '合成摘要', description: '合成描述', issue_note: '合成缺口', relation_code: 'synthetic-relation', match_type: 'account_transfer', from_account_code: 'fixture/cny', to_account_code: 'fixture/hkd', counterparty: '合成对方'}],
};
export const imports = {
  imports: [{imported_at: '2026-10-30 12:00:00', profile_code: 'synthetic_cny', account_code: 'fixture/cny', source_file: '/synthetic/accounts.csv', row_count: 9, statement_balance: 1000}],
  summary: [{profile_code: 'synthetic_cny', row_count: 9, latest_balance: 999, net_asset_balance: 1000, inflow: 120, outflow: 20, net_flow: 100, latest_balance_date: '2026-10-29', balance_source: 'holding_snapshot'}],
};
export const initialUrls = ['/api/bank-reconcile/config', '/api/accounts/overview', '/api/bank-reconcile/transfer-matches', '/api/bank-reconcile/unmatched-rows?since=2026-01-01&limit=500', '/api/bank-reconcile/imports'];
export const responseFor = url => url.includes('/config') ? config : url.includes('/overview') ? overview : url.includes('/unmatched-rows') ? unmatched : url.includes('/imports') ? imports : matches;
