#!/usr/bin/env python3
"""
从资产负债表.xlsx导入最新持仓数据到pams_holdings_snapshot
"""
import openpyxl
import psycopg2
from datetime import date
import sys

# 账户映射：Excel显示名称 -> 数据库account_code
ACCOUNT_MAPPING = {
    '腾讯理财通': 'tencent_licaitong',
    '建行': 'jianhang',
    '同花顺': 'tonghuashun',
    '华为账户': 'huawei',
    '公积金账户': 'gongjijin',
    '币安': 'binance',
    '借款': 'jiekuan',
    '广发': 'guangfa',
    '微信': 'wechat',
    'Santander': 'santander',
    'IBKR': 'ibkr'
}

def parse_cost(cost_value):
    """解析成本字段（可能是公式字符串）"""
    if cost_value is None:
        return None
    if isinstance(cost_value, (int, float)):
        return float(cost_value)
    # 处理公式字符串，如 "=11853.8-3992.56"
    if isinstance(cost_value, str) and cost_value.startswith('='):
        try:
            return eval(cost_value[1:])  # 移除=号并计算
        except:
            return None
    return float(cost_value)

def import_holdings(excel_path, db_config, as_of_date=None):
    """导入持仓数据"""
    if as_of_date is None:
        as_of_date = date.today()

    # 读取Excel
    wb = openpyxl.load_workbook(excel_path)
    ws = wb.active

    # 连接数据库
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor()

    imported_count = 0
    error_count = 0

    # 跳过标题行，从第2行开始
    for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row[0]:  # 跳过空行
            continue

        name = row[0]           # 标的名称
        asset_liability = row[1] # 资产/负债
        currency = row[2]       # 币种
        account = row[3]        # 账户
        major_cat = row[4]      # 大类
        minor_cat = row[5]      # 小类
        market_value = row[6]   # 当前市值
        cost = parse_cost(row[7])  # 成本

        # 映射账户名称
        account_code = ACCOUNT_MAPPING.get(account)
        if not account_code:
            print(f'警告: 第{row_num}行账户"{account}"未映射，跳过')
            error_count += 1
            continue

        try:
            cur.execute("""
                INSERT INTO pams_holdings_snapshot (
                    symbol, name, asset_liability, currency, account_code,
                    major_category, minor_category, market_value, cost, as_of
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_code, name, as_of)
                DO UPDATE SET
                    market_value = EXCLUDED.market_value,
                    cost = EXCLUDED.cost,
                    updated_at = NOW()
            """, (
                None,  # symbol (Excel中未提供)
                name,
                asset_liability,
                currency,
                account_code,
                major_cat,
                minor_cat,
                market_value,
                cost,
                as_of_date
            ))
            imported_count += 1
        except Exception as e:
            print(f'错误: 第{row_num}行插入失败 - {e}')
            error_count += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f'\n导入完成：成功 {imported_count} 条，失败 {error_count} 条')
    return imported_count, error_count

if __name__ == '__main__':
    DB_CONFIG = {
        'host': 'localhost',
        'port': 5432,
        'database': 'pams',
        'user': 'pams',
        'password': 'pams'  # 从环境变量读取更安全
    }

    EXCEL_PATH = '/tmp/balance.xlsx'

    try:
        import_holdings(EXCEL_PATH, DB_CONFIG)
    except Exception as e:
        print(f'导入失败: {e}', file=sys.stderr)
        sys.exit(1)
