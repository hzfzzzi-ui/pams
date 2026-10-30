"""
历史收支交易与预算接口；Telegram 记账入口已停用。
"""
from typing import Optional
from datetime import datetime
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

# ==================== API端点 ====================

@router.post("/transactions")
def create_transaction(tx: TransactionCreate):
    """创建非 Telegram 来源的旧交易记录。"""
    if tx.source == "telegram":
        raise HTTPException(status_code=410, detail="Telegram 记账入口已停用，请使用 PAMS 资料导入。")
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

    return {
        "success": True,
        "transaction_id": transaction_id,
        "message": "交易记录已创建"
    }

def _require_cny_amounts(cur, start_date: str, end_date: str) -> None:
    """Reject incomplete legacy aggregates instead of inventing CNY amounts."""
    cur.execute("""
        SELECT COUNT(*) AS missing_count
        FROM pams_transactions t
        LEFT JOIN LATERAL (
            SELECT rate FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY' AND quote_currency = t.currency
              AND as_of <= t.date
            ORDER BY as_of DESC LIMIT 1
        ) fx ON TRUE
        WHERE t.date >= %s::date AND t.date <= %s::date
          AND (t.amount IS NULL OR t.currency IS NULL OR t.currency = ''
               OR (UPPER(t.currency) <> 'CNY' AND (fx.rate IS NULL OR fx.rate <= 0)))
    """, (start_date, end_date))
    if cur.fetchone()["missing_count"]:
        raise HTTPException(status_code=422, detail="Missing exchange rate or transaction amount; cannot calculate CNY totals")


@router.get("/transactions/recent")
def get_recent_transactions(limit: int = Query(50, ge=1, le=200)):
    """获取最近交易（含CNY换算金额）"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    t.id, t.date, t.amount, t.currency, t.category, t.subcategory,
                    t.account_code, t.description, t.location, t.auto_classified,
                    t.source, t.created_at,
                    a.display_name as account_name,
                    ROUND((t.amount * (CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END))::numeric, 2) as amount_cny
                FROM pams_transactions t
                LEFT JOIN pams_accounts a ON a.account_code = t.account_code
                LEFT JOIN LATERAL (
            SELECT rate FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY' AND quote_currency = t.currency
              AND as_of <= t.date
            ORDER BY as_of DESC LIMIT 1
        ) fx ON TRUE
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
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _require_cny_amounts(cur, start_date, end_date)
            # 总收入/支出（含汇率换算到CNY）
            cur.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount * (CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END) ELSE 0 END), 0) as total_income,
                    COALESCE(SUM(CASE WHEN t.amount < 0 THEN ABS(t.amount * (CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END)) ELSE 0 END), 0) as total_expense,
                    COALESCE(SUM(t.amount * (CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END)), 0) as net
                FROM pams_transactions t
                LEFT JOIN LATERAL (
            SELECT rate FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY' AND quote_currency = t.currency
              AND as_of <= t.date
            ORDER BY as_of DESC LIMIT 1
        ) fx ON TRUE
                WHERE t.date >= %s::date AND t.date <= %s::date
            """, (start_date, end_date))
            totals = cur.fetchone()

            # 按分类统计支出（含汇率换算）
            cur.execute("""
                SELECT category,
                       ROUND(SUM(ABS(t.amount * (CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END)))::numeric, 2) as amount,
                       COUNT(*) as count
                FROM pams_transactions t
                LEFT JOIN LATERAL (
            SELECT rate FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY' AND quote_currency = t.currency
              AND as_of <= t.date
            ORDER BY as_of DESC LIMIT 1
        ) fx ON TRUE
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
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _require_cny_amounts(cur, start_date, end_date)
            cur.execute("""
                SELECT
                    DATE_PART('day', t.date)::int as day,
                    COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount * (CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END) ELSE 0 END), 0) as income,
                    COALESCE(SUM(CASE WHEN t.amount < 0 THEN ABS(t.amount * (CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END)) ELSE 0 END), 0) as expense
                FROM pams_transactions t
                LEFT JOIN LATERAL (
            SELECT rate FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY' AND quote_currency = t.currency
              AND as_of <= t.date
            ORDER BY as_of DESC LIMIT 1
        ) fx ON TRUE
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
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _require_cny_amounts(cur, start, end)
            cur.execute("""
                SELECT category,
                       ROUND(SUM(ABS(t.amount * (CASE WHEN UPPER(t.currency) = 'CNY' THEN 1 WHEN fx.rate > 0 THEN fx.rate END)))::numeric, 2) as amount,
                       COUNT(*) as count
                FROM pams_transactions t
                LEFT JOIN LATERAL (
            SELECT rate FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY' AND quote_currency = t.currency
              AND as_of <= t.date
            ORDER BY as_of DESC LIMIT 1
        ) fx ON TRUE
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
