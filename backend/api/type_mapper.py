"""
PAMS 资产类型映射模块

将数据库中的type字段映射到资产负债表的大类/小类
"""

# 大类定义（四个子页面分类）
MAJOR_CATEGORIES = {
    'A股': ['A股', '分红'],  # A股页面：包含A股和分红类
    '美股': ['美股'],
    '加密': ['加密'],
    '其他': ['贵金属', '理财', '现金', '公积金', '借款', '欠款', '应收款']
}

# 数据库type字段 -> 大类映射
TYPE_TO_MAJOR = {
    # A股相关
    'stock': 'A股',
    'stock_cn': 'A股',
    'fund': 'A股',  # 默认基金归为A股
    'equity_cn': 'A股',

    # 美股相关
    'stock_us': '美股',
    'equity_us': '美股',

    # 加密相关
    'crypto': '加密',

    # 其他类别
    'gold': '贵金属',
    'asset_gold': '贵金属',
    'wealth': '理财',
    'cash': '现金',
    'hpf': '公积金',
    'asset_hpf': '公积金',
    'loan': '借款',
    'recv_loan': '借款',
    'receivable_loan': '借款',  # 借款应收
    'receivable': '应收款',
    'recv': '应收款',
    'recv_reimburse': '应收款',
    'receivable_reimburse': '应收款',
    'liability': '欠款',
    'liability_cc': '欠款',  # 信用卡负债
    'esop': '内部持股',  # 员工持股计划
    'equity_emp': '内部持股',
    'equity_private': '内部持股',  # 华为员工内部持股
}

# 数据库type字段 -> 小类映射
TYPE_TO_MINOR = {
    # A股
    'stock': 'A股',
    'stock_cn': 'A股',
    'fund': 'A股基金',
    'equity_cn': 'A股权益',

    # 美股
    'stock_us': '美股',
    'equity_us': '美股权益',

    # 加密
    'crypto': 'ETH',  # 可扩展为BTC, ETH等

    # 贵金属
    'gold': '黄金',
    'asset_gold': '黄金',

    # 理财
    'wealth': '固收理财',

    # 现金
    'cash': '现金/活期',

    # 公积金
    'hpf': '现金/活期',
    'asset_hpf': '现金/活期',

    # 借款
    'loan': '借款',
    'recv_loan': '借款',
    'receivable_loan': '借款',

    # 应收款
    'receivable': '应收款',
    'recv': '应收款',
    'recv_reimburse': '应收款',
    'receivable_reimburse': '应收款',

    # 欠款
    'liability': '信用卡',
    'liability_cc': '信用卡',

    # 内部持股
    'esop': '员工持股',
    'equity_emp': '员工持股',
    'equity_private': '员工持股',
}


def get_major_category(type_value: str) -> str:
    """
    获取大类分类

    Args:
        type_value: 数据库type字段值

    Returns:
        大类名称 (A股/美股/加密/其他)
    """
    if not type_value:
        return '其他'

    # 直接映射
    major = TYPE_TO_MAJOR.get(type_value)
    if major:
        # 检查是否属于A股页面的两个大类
        if major in ['A股', '分红']:
            return 'A股'
        # 检查是否属于其他页面
        elif major in ['贵金属', '理财', '现金', '公积金', '借款', '欠款', '应收款']:
            return '其他'
        else:
            return major

    # 未匹配，默认归为其他
    return '其他'


def get_minor_category(type_value: str) -> str:
    """
    获取小类分类

    Args:
        type_value: 数据库type字段值

    Returns:
        小类名称
    """
    if not type_value:
        return ''

    return TYPE_TO_MINOR.get(type_value, type_value)


def filter_by_page(rows, page_name: str):
    """
    按页面类型筛选持仓数据

    Args:
        rows: 持仓数据列表
        page_name: 页面名称 ('A股', '美股', '加密', '其他')

    Returns:
        筛选后的数据列表
    """
    filtered = []
    for row in rows:
        type_value = row.get('type', '')
        major = get_major_category(type_value)
        if major == page_name:
            # 添加大类和小类字段
            row['major_category'] = major
            row['minor_category'] = get_minor_category(type_value)
            filtered.append(row)

    return filtered


def get_page_categories():
    """
    获取四个子页面的分类列表

    Returns:
        ['A股', '美股', '加密', '其他']
    """
    return ['A股', '美股', '加密', '其他']
