"""
PAMS 账户标准化映射模块

将数据库中的各种账户ID统一映射到标准的11个账户名称
"""

# 标准账户列表（基于资产负债表.xlsx）
STANDARD_ACCOUNTS = [
    '腾讯理财通',
    '建行',
    '华为账户',
    '公积金账户',
    '币安',
    'IBKR',
    '广发',
    'Santander',
    '同花顺',
    '借款',
    '微信'
]

# 账户映射表：数据库账户ID -> 标准账户名称
ACCOUNT_MAPPING = {
    # 腾讯理财通
    'tencent_licaitong': '腾讯理财通',
    'licaitong': '腾讯理财通',
    'main': '腾讯理财通',
    '主账户': '腾讯理财通',

    # 建行
    'ccb': '建行',
    'ccb_wm47': '建行',
    'ccb_gold': '建行',
    'ccb_7332': '建行',
    '建行': '建行',

    # 华为账户
    'huawei': '华为账户',
    'huawei_emp': '华为账户',
    'huawei_esop': '华为账户',
    '华为账户': '华为账户',

    # 公积金账户
    'cn_hpf': '公积金账户',
    'hpf': '公积金账户',
    '公积金账户': '公积金账户',

    # 币安
    'binance': '币安',
    '币安': '币安',

    # IBKR
    'ibkr': 'IBKR',
    'us_main': 'IBKR',
    'IBKR': 'IBKR',
    '美股主账户': 'IBKR',

    # 广发
    'gf': '广发',
    'guangfa': '广发',
    'gf_7443': '广发',
    'gf_9711': '广发',
    '广发': '广发',

    # Santander
    'santander': 'Santander',
    'santander4209': 'Santander',
    'santander4209_cash': 'Santander',
    'Santander': 'Santander',
    '美股主账户': 'Santander',

    # 同花顺
    'tonghuashun': '同花顺',
    'ths': '同花顺',
    'cn_a': '同花顺',
    'cn_a_rmb': '同花顺',
    '同花顺': '同花顺',
    'A股人民币账户': '同花顺',

    # 借款
    'recv_loan': '借款',
    'loan': '借款',
    '借款': '借款',

    # 微信
    'wechat': '微信',
    'weixin': '微信',
    '微信': '微信',

    # 其他
    'recv_reimburse': '其他',
    'manual': '其他',
    'unknown': '其他',
}


def standardize_account(account_id: str) -> str:
    """
    将账户ID标准化为11个标准账户之一

    Args:
        account_id: 数据库中的账户ID

    Returns:
        标准账户名称，如果无法映射则返回'其他'
    """
    if not account_id:
        return '其他'

    # 尝试直接映射
    if account_id in ACCOUNT_MAPPING:
        return ACCOUNT_MAPPING[account_id]

    # 如果已经是标准账户名称，直接返回
    if account_id in STANDARD_ACCOUNTS:
        return account_id

    # 模糊匹配（包含关系）
    account_lower = account_id.lower()
    for key, value in ACCOUNT_MAPPING.items():
        if key.lower() in account_lower or account_lower in key.lower():
            return value

    # 无法映射，返回'其他'
    return '其他'


def get_account_filter_options():
    """
    获取账户筛选选项列表

    Returns:
        标准账户列表
    """
    return STANDARD_ACCOUNTS.copy()


def validate_account(account_name: str) -> bool:
    """
    验证账户名称是否为标准账户

    Args:
        account_name: 账户名称

    Returns:
        True if valid, False otherwise
    """
    return account_name in STANDARD_ACCOUNTS or account_name == '其他'
