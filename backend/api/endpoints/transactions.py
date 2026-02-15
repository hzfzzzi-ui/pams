"""
交易记录API V2 - 支持收支同步到holdings现金持仓
"""
from typing import Optional
from datetime import date as Date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()

# 账户币种映射
ACCOUNT_CURRENCY_MAP = {
    'santander': 'MXN',
    'ibkr': 'MXN',
    'binance': 'USD',
    # 其他账户默认CNY
}

def get_conn():
    import os
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )

def _norm(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    from decimal import Decimal
    if isinstance(v, Decimal):
        return str(v)
    return v

def get_account_currency(account_code: str) -> str:
    """根据account_code获取币种"""
    return ACCOUNT_CURRENCY_MAP.get(account_code, 'CNY')

# ==================== 数据模型 ====================

class TransactionCreate(BaseModel):
    date: str  # YYYY-MM-DD
    amount: float
    currency: Optional[str] = None  # 如果不提供，根据account自动推断
    account_code: str
    category: Optional[str] = None
    subcategory: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    auto_classified: bool = False
    source: str = "telegram"

class BudgetCreate(BaseModel):
    category: str
    month: str  # YYYY-MM
    amount: float
    currency: str = "CNY"
    alert_threshold: float = 0.8

# ==================== 同步逻辑 ====================

def sync_transaction_to_holdings(
    conn,
    transaction_id: int,
    account_code: str,
    amount: float,
    currency: str,
    tx_date: Date,
    description: str
):
    """
    同步交易到holdings表的现金持仓
    - 收入：增加现金持仓
    - 支出：减少现金持仓
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 检查该账户是否有现金持仓记录
        cur.execute("""
            SELECT id, market_value
            FROM pams_holdings_snapshot
            WHERE account_code = %s
              AND minor_category = '现金/活期'
              AND as_of = %s
        """, (account_code, tx_date))

        holding = cur.fetchone()

        if holding:
            # 更新现有持仓
            new_value = float(holding['market_value']) + amount
            cur.execute("""
                UPDATE pams_holdings_snapshot
                SET market_value = %s,
                    cost = market_value,  -- 现金成本=市值
                    updated_at = NOW()
                WHERE id = %s
            """, (new_value, holding['id']))

            holding_id = holding['id']
        else:
            # 创建新的现金持仓记录
            # 获取账户显示名称
            cur.execute("SELECT display_name FROM pams_accounts WHERE account_code = %s", (account_code,))
            acc_row = cur.fetchone()
            display_name = acc_row['display_name'] if acc_row else account_code

            cash_name = f"{display_name}现金"

            cur.execute("""
                INSERT INTO pams_holdings_snapshot (
                    symbol, name, asset_liability, currency, account_code,
                    major_category, minor_category, market_value, cost, as_of
                ) VALUES (
                    NULL, %s, '资产', %s, %s, '现金', '现金/活期', %s, %s, %s
                )
                RETURNING id
            """, (cash_name, currency, account_code, amount, amount, tx_date))

            holding_id = cur.fetchone()['id']

        # 记录同步日志（调试用）
        sync_type = 'income' if amount > 0 else 'expense'
        cur.execute("""
            INSERT INTO pams_transaction_sync_log (
                transaction_id, holding_id, sync_type, amount
            ) VALUES (%s, %s, %s, %s)
        """, (transaction_id, holding_id, sync_type, abs(amount)))

        conn.commit()

# ==================== API端点 ====================

@router.post("/transactions")
def create_transaction(tx: TransactionCreate):
    """创建交易记录并同步到holdings"""
    # 自动推断币种
    if not tx.currency:
        tx.currency = get_account_currency(tx.account_code)

    # 解析日期
    try:
        tx_date = datetime.strptime(tx.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误，应为YYYY-MM-DD")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 验证账户存在
            cur.execute("SELECT account_code FROM pams_accounts WHERE account_code = %s", (tx.account_code,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail=f"账户 {tx.account_code} 不存在")

            # 自动分类（如果未提供category）
            if not tx.category and tx.description:
                cur.execute("""
                    SELECT category, subcategory
                    FROM pams_category_rules
                    WHERE active = true AND %s ILIKE '%%' || keyword || '%%'
                    ORDER BY priority ASC, id ASC
                    LIMIT 1
                """, (tx.description,))
                rule = cur.fetchone()
                if rule:
                    tx.category = rule['category']
                    tx.subcategory = rule['subcategory']
                    tx.auto_classified = True

                    # 更新匹配次数
                    cur.execute("""
                        UPDATE pams_category_rules
                        SET match_count = match_count + 1
                        WHERE category = %s AND subcategory = %s
                    """, (tx.category, tx.subcategory))

            # 插入交易记录
            cur.execute("""
                INSERT INTO pams_transactions (
                    date, amount, currency, category, subcategory,
                    account_code, description, location,
                    auto_classified, source, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
                )
                RETURNING id
            """, (
                tx_date, tx.amount, tx.currency, tx.category, tx.subcategory,
                tx.account_code, tx.description, tx.location,
                tx.auto_classified, tx.source
            ))

            transaction_id = cur.fetchone()['id']
            conn.commit()

        # 同步到holdings（仅Telegram来源的收支）
        if tx.source == "telegram":
            sync_transaction_to_holdings(
                conn, transaction_id, tx.account_code,
                tx.amount, tx.currency, tx_date, tx.description or ""
            )

    return {
        "success": True,
        "transaction_id": transaction_id,
        "message": f"交易记录已创建{'并同步到持仓' if tx.source == 'telegram' else ''}"
    }

@router.get("/transactions/recent")
def get_recent_transactions(limit: int = Query(50, ge=1, le=200)):
    """获取最近交易（含CNY换算金额）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest_fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE base_currency = 'CNY'
                      AND as_of = (SELECT MAX(as_of) FROM pams_fx_rates_daily WHERE base_currency = 'CNY')
                )
                SELECT
                    t.id, t.date, t.amount, t.currency, t.category, t.subcategory,
                    t.account_code, t.description, t.location, t.auto_classified,
                    t.source, t.created_at,
                    a.display_name as account_name,
                    ROUND((t.amount * COALESCE(fx.rate, 1))::numeric, 2) as amount_cny
                FROM pams_transactions t
                LEFT JOIN pams_accounts a ON a.account_code = t.account_code
                LEFT JOIN latest_fx fx ON fx.quote_currency = t.currency
                ORDER BY t.date DESC, t.created_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()

    return {"transactions": [{k: _norm(v) for k, v in dict(r).items()} for r in rows]}

@router.get("/transactions/monthly/{month}")
def get_monthly_stats(month: str):
    """获取月度统计（YYYY-MM）"""
    try:
        year, mon = month.split('-')
        start_date = f"{year}-{mon}-01"
        # 计算月末日期
        import calendar
        last_day = calendar.monthrange(int(year), int(mon))[1]
        end_date = f"{year}-{mon}-{last_day}"
    except:
        raise HTTPException(status_code=400, detail="月份格式错误，应为YYYY-MM")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 总收入/支出（含汇率换算到CNY）
            cur.execute("""
                WITH latest_fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE base_currency = 'CNY'
                      AND as_of = (SELECT MAX(as_of) FROM pams_fx_rates_daily WHERE base_currency = 'CNY')
                )
                SELECT
                    COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount * COALESCE(fx.rate, 1) ELSE 0 END), 0) as total_income,
                    COALESCE(SUM(CASE WHEN t.amount < 0 THEN ABS(t.amount * COALESCE(fx.rate, 1)) ELSE 0 END), 0) as total_expense,
                    COALESCE(SUM(t.amount * COALESCE(fx.rate, 1)), 0) as net
                FROM pams_transactions t
                LEFT JOIN latest_fx fx ON fx.quote_currency = t.currency
                WHERE t.date >= %s::date AND t.date <= %s::date
            """, (start_date, end_date))
            totals = cur.fetchone()

            # 按分类统计支出（含汇率换算）
            cur.execute("""
                WITH latest_fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE base_currency = 'CNY'
                      AND as_of = (SELECT MAX(as_of) FROM pams_fx_rates_daily WHERE base_currency = 'CNY')
                )
                SELECT category,
                       ROUND(SUM(ABS(t.amount * COALESCE(fx.rate, 1)))::numeric, 2) as amount,
                       COUNT(*) as count
                FROM pams_transactions t
                LEFT JOIN latest_fx fx ON fx.quote_currency = t.currency
                WHERE t.date >= %s::date AND t.date <= %s::date AND t.amount < 0
                GROUP BY category
                ORDER BY amount DESC
            """, (start_date, end_date))
            by_category = cur.fetchall()

    return {
        "month": month,
        "total_income": str(totals['total_income']),
        "total_expense": str(totals['total_expense']),
        "net": str(totals['net']),
        "by_category": [{k: _norm(v) for k, v in dict(r).items()} for r in by_category]
    }

@router.get("/transactions/daily/{month}")
def get_daily_stats(month: str):
    """获取每日统计"""
    try:
        year, mon = month.split('-')
        start_date = f"{year}-{mon}-01"
        import calendar
        last_day = calendar.monthrange(int(year), int(mon))[1]
        end_date = f"{year}-{mon}-{last_day}"
    except:
        raise HTTPException(status_code=400, detail="月份格式错误")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest_fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE base_currency = 'CNY'
                      AND as_of = (SELECT MAX(as_of) FROM pams_fx_rates_daily WHERE base_currency = 'CNY')
                )
                SELECT
                    DATE_PART('day', t.date)::int as day,
                    COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount * COALESCE(fx.rate, 1) ELSE 0 END), 0) as income,
                    COALESCE(SUM(CASE WHEN t.amount < 0 THEN ABS(t.amount * COALESCE(fx.rate, 1)) ELSE 0 END), 0) as expense
                FROM pams_transactions t
                LEFT JOIN latest_fx fx ON fx.quote_currency = t.currency
                WHERE t.date >= %s::date AND t.date <= %s::date
                GROUP BY DATE_PART('day', t.date)
                ORDER BY day
            """, (start_date, end_date))
            rows = cur.fetchall()

    return [{k: _norm(v) for k, v in dict(r).items()} for r in rows]

@router.get("/transactions/categories-by-range")
def get_categories_by_range(start: str, end: str):
    """获取时间段内的分类占比（用于图表联动，含汇率换算）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest_fx AS (
                    SELECT quote_currency, rate
                    FROM pams_fx_rates_daily
                    WHERE base_currency = 'CNY'
                      AND as_of = (SELECT MAX(as_of) FROM pams_fx_rates_daily WHERE base_currency = 'CNY')
                )
                SELECT category,
                       ROUND(SUM(ABS(t.amount * COALESCE(fx.rate, 1)))::numeric, 2) as amount,
                       COUNT(*) as count
                FROM pams_transactions t
                LEFT JOIN latest_fx fx ON fx.quote_currency = t.currency
                WHERE t.date >= %s::date AND t.date <= %s::date AND t.amount < 0
                GROUP BY category
                ORDER BY amount DESC
            """, (start, end))
            rows = cur.fetchall()

    return [{k: _norm(v) for k, v in dict(r).items()} for r in rows]

@router.get("/transactions/budgets/{month}")
def get_budgets(month: str):
    """获取预算执行情况"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT b.category, b.budget_amount, b.currency, b.alert_threshold,
                       COALESCE(SUM(ABS(t.amount)), 0) as actual_expense
                FROM pams_budgets b
                LEFT JOIN pams_transactions t
                    ON t.category = b.category
                    AND TO_CHAR(t.date, 'YYYY-MM') = b.month
                    AND t.amount < 0
                WHERE b.month = %s::date
                GROUP BY b.category, b.budget_amount, b.currency, b.alert_threshold
            """, (f"{month}-01",))
            rows = cur.fetchall()

    budgets = []
    for r in rows:
        budget_dict = {k: _norm(v) for k, v in dict(r).items()}
        budget_dict['usage_pct'] = (float(r['actual_expense']) / float(r['budget_amount'])) if r['budget_amount'] > 0 else 0
        budget_dict['alert'] = budget_dict['usage_pct'] >= float(r['alert_threshold'])
        budgets.append(budget_dict)

    return {"budgets": budgets}

@router.post("/transactions/budgets")
def create_budget(budget: BudgetCreate):
    """创建预算"""
    try:
        month_date = f"{budget.month}-01"
        datetime.strptime(month_date, "%Y-%m-%d")
    except:
        raise HTTPException(status_code=400, detail="月份格式错误")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pams_budgets (category, month, budget_amount, currency, alert_threshold)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (category, month) DO UPDATE
                SET budget_amount = EXCLUDED.budget_amount,
                    currency = EXCLUDED.currency,
                    alert_threshold = EXCLUDED.alert_threshold
            """, (budget.category, month_date, budget.amount, budget.currency, budget.alert_threshold))
            conn.commit()

    return {"success": True, "message": "预算已创建"}
