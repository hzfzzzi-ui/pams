"""Account names and institution groups shared by account pickers and summaries."""


_GROUP_OVERRIDES = {
    'santander': ('santander', 'Santander 4209 储蓄卡'),
    'alipay': ('alipay', '支付宝余额'),
    'ibkr': ('ibkr', 'IBKR 证券账户'),
    'tonghuashun': ('tonghuashun', '同花顺 A 股证券账户'),
    'wechat': ('tencent', '微信支付账单（全渠道）'),
    'wechat_balance': ('tencent', None),
    'wechat_lqt': ('tencent', None),
    'tencent_licaitong': ('tencent', None),
    'huawei_stock': ('ccb', None),
}


def account_display(config, code, fallback_label):
    account = config.get('accounts', {}).get(code, {})
    group_code, label = _GROUP_OVERRIDES.get(code, (account.get('parent_account_code') or code, None))
    root = config.get('account_roots', {}).get(group_code, {})
    label = label or account.get('display_name') or fallback_label
    group_label = '腾讯' if group_code == 'tencent' else root.get('display_name')
    return group_code, group_label, label
