export const ROOT = '/api/account-import';
export const KIND_TEXT = {trade:'交易',holding:'持仓快照',cash:'账单'};
export const DECIMALS = ['quantity', 'price', 'gross_amount', 'fee_amount', 'tax_amount', 'net_cash', 'market_value', 'cost'];
export const LABELS = { kind: '类型', business_date: '业务日期', trade_time: '成交时间', confirmed_date: '确认日期', asset_type: '标的类型', symbol: '代码', name: '名称', currency: '币种', action: '方向', status: '状态', quantity: '数量 / 份数', price: '价格', gross_amount: '发生金额', fee_scope: '费用范围', fee_amount: '费用金额', tax_amount: '税款', net_cash: '现金金额 / 余额', market_value: '市值', cost: '成本', cost_basis_type: '成本口径', counter_account_code: '对手账户', source_ref: '成交/流水号', note: '业务备注', duplicate_decision: '重复判定', asset_id: '标的 ID' };
export const PREVIEW_LABELS = { account_code: '账户', as_of: '快照日期', ...LABELS, current_price: '现价', buy_price: '成本均价', major_category: '资产大类', minor_category: '资产子类', asset_liability: '资产/负债',
  event_date:'发生日期',posting_date:'入账日期',amount:'原币金额',amount_cny:'人民币金额',fx_rate_to_cny:'采用汇率',cashflow_type:'资金类型',category:'分类',subcategory:'子类',counterparty:'对方',summary:'摘要',description:'描述',affects_nav:'影响NAV',affects_spending:'计入收支',affects_investment_capital:'计入投资本金',is_internal_transfer:'内部划转',net_cash:'净收付 / 余额' };
export const TEXT = { trade: '交易', holding: '持仓快照', cash: '现金', stock: '股票', fund: '基金', buy: '买入', sell: '卖出', subscribe: '申购', redeem: '赎回', dividend: '分红', fee: '费用', tax: '税款', interest: '利息', balance: '余额', deposit: '入金', withdraw: '出金', transfer: '划转', pending: '待确认', confirmed: '已确认', ready: '可发布', duplicate: '重复，跳过', blocked: '阻断', draft: '草稿', published: '已发布', voided: '已作废', partial: '部分记录', complete: '完整快照', holding_cost: '持仓成本', diluted_cost: '摊薄成本', unknown: '未知', total_charges: '税费合计', separate: '手续费与税分列', distinct: '核对为不同交易', skip: '跳过此条' };

export function decimalString(value) {
  const text = String(value ?? '').trim();
  if (text && !/^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)) throw new Error('金额与数量须为十进制数字，不接受千分位或科学计数法');
  return text;
}
export function fileFormat(name, kind = 'trade') {
  const format = String(name).match(/\.([^.]+)$/)?.[1]?.toLowerCase();
  if (![...['csv', 'tsv', 'xls', 'xlsx', 'png', 'jpg', 'jpeg'], ...(kind === 'cash' ? ['pdf'] : [])].includes(format)) throw new Error('仅支持 CSV、TSV、同花顺 XLS、XLSX、PNG、JPG、JPEG；账单另支持 PDF。');
  return format;
}
export function previewFields(object) {
  return Object.entries(PREVIEW_LABELS)
    .filter(([key]) => key !== 'asset_id' && Object.hasOwn(object || {}, key))
    .filter(([key]) => object[key] != null && (typeof object[key] !== 'string' || object[key].trim() !== ''))
    .map(([key, label]) => ({ key, label, value: object[key] }));
}
export function accessState(caps) {
  return caps?.schema_ready === true ? 'ready' : 'unavailable';
}
export function displayAssetType(type) {
  return ['fund', 'etf', 'mutual_fund'].includes(type) ? 'fund' : ['stock', 'equity', 'listed_equity'].includes(type) ? 'stock' : type;
}
export function recordEditable(row) {
  return !row || row.can_edit_fields === true || (row.can_edit_fields === undefined && (!row.source?.file_id || row.source?.origin === 'ocr'));
}
export function publishable(caps, batch, preview, dirty = false) {
  return caps?.publish_enabled === true && batch?.status === 'draft' && !dirty && preview?.can_publish === true &&
    preview.batch_id === batch.batch_id && preview.revision === batch.revision && !!preview.preview_token &&
    preview.counts?.blocked === 0 && preview.counts?.pending === 0 && !preview.rows?.some(row => ['blocked', 'pending'].includes(row.status));
}
