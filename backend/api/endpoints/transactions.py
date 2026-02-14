"""
PAMS 收支记账 API 端点

实现功能：
- 创建交易记录
- 查询交易记录
- 月度统计
- 自动分类
- 预算管理
"""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import Optional, List
from decimal import Decimal
import asyncpg
import os
import traceback

router = APIRouter(prefix="/transactions", tags=["transactions"])

# 数据库连接配置
DB_CONFIG = {
    'host': os.getenv('PGHOST', 'localhost'),
    'port': int(os.getenv('PGPORT', 5432)),
    'database': os.getenv('PGDATABASE', 'pams'),
    'user': os.getenv('PGUSER', 'pams'),
    'password': os.getenv('PGPASSWORD', os.getenv('POSTGRES_PASSWORD', ''))
}


async def get_db_pool():
    """获取数据库连接池"""
    return await asyncpg.create_pool(**DB_CONFIG)


# ===== 数据模型 =====

class TransactionCreate(BaseModel):
    """创建交易请求"""
    amount: float = Field(..., description="金额（正数=收入，负数=支出）")
    category: str = Field(..., description="分类")
    description: Optional[str] = Field(None, description="备注")
    date: Optional[str] = Field(None, description="日期 YYYY-MM-DD")
    location: Optional[str] = Field(None, description="地点")
    account: Optional[str] = Field("Santander", description="账户")
    currency: str = Field("MXN", description="币种")
    source: str = Field("manual", description="数据来源")


class TransactionResponse(BaseModel):
    """交易响应"""
    id: int
    date: date
    amount: float
    category: str
    subcategory: Optional[str]
    description: Optional[str]
    location: Optional[str]
    auto_classified: bool
    account: Optional[str]
    currency: str
    source: str
    created_at: datetime


class MonthlyStats(BaseModel):
    """月度统计"""
    month: str
    total_income: float
    total_expense: float
    net: float
    by_category: Optional[List[dict]] = []


# ===== 自动分类引擎 =====

async def auto_classify(description: str, pool) -> tuple[str, Optional[str], float]:
    """
    根据描述自动分类

    返回: (category, subcategory, confidence)
    """
    if not description:
        return ("其他", None, 0.0)

    # 查询匹配规则
    query = """
        SELECT category, subcategory, priority
        FROM pams_category_rules
        WHERE active = TRUE
          AND $1 ILIKE '%' || keyword || '%'
        ORDER BY priority DESC, LENGTH(keyword) DESC
        LIMIT 1;
    """

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, description)

        if row:
            # 更新匹配计数
            await conn.execute(
                "UPDATE pams_category_rules SET match_count = match_count + 1 WHERE category = $1 AND keyword = $2",
                row['category'], description
            )
            return (row['category'], row['subcategory'], 0.9)

    # 无匹配规则
    return ("其他", None, 0.0)


# ===== API 端点 =====

@router.post("/test")
async def test_endpoint(request: Request):
    """测试端点 - 验证请求体接收"""
    try:
        body = await request.json()
        return {"received": body, "status": "ok"}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


@router.post("", response_model=TransactionResponse)
async def create_transaction(tx: TransactionCreate):
    """
    创建新交易记录

    - 自动分类识别
    - 数据验证
    - 存入数据库
    """
    print(f"[DEBUG] Received transaction: {tx}")  # Debug logging
    pool = await get_db_pool()

    try:
        # 自动分类（如果有备注）
        if tx.description:
            category, subcategory, confidence = await auto_classify(tx.description, pool)
            auto_classified = confidence > 0.6
            if auto_classified:
                tx.category = category
        else:
            subcategory = None
            confidence = 0.0
            auto_classified = False

        # 存入数据库
        query = """
            INSERT INTO pams_transactions
            (date, amount, currency, category, subcategory, description,
             location, account, auto_classified, confidence, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *;
        """

        # 处理日期
        if tx.date:
            transaction_date = datetime.strptime(tx.date, '%Y-%m-%d').date()
        else:
            transaction_date = date.today()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                transaction_date,
                float(tx.amount),
                tx.currency,
                tx.category,
                subcategory,
                tx.description,
                tx.location,
                tx.account,
                auto_classified,
                confidence,
                tx.source
            )

        return TransactionResponse(
            id=row['id'],
            date=row['date'],
            amount=float(row['amount']),
            category=row['category'],
            subcategory=row['subcategory'],
            description=row['description'],
            location=row['location'],
            auto_classified=row['auto_classified'],
            account=row['account'],
            currency=row['currency'],
            source=row['source'],
            created_at=row['created_at']
        )

    except Exception as e:
        print(f"[ERROR] Create transaction failed: {str(e)}")  # Debug logging
        print(f"[ERROR] Traceback: {traceback.format_exc()}")  # Full traceback
        raise HTTPException(status_code=500, detail=f"创建交易失败: {str(e)}")
    finally:
        await pool.close()


@router.get("/recent", response_model=List[TransactionResponse])
async def get_recent_transactions(limit: int = Query(50, ge=1, le=500)):
    """获取最近交易记录"""
    pool = await get_db_pool()

    try:
        query = """
            SELECT * FROM pams_transactions
            ORDER BY date DESC, created_at DESC
            LIMIT $1;
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, limit)

        return [
            TransactionResponse(
                id=row['id'],
                date=row['date'],
                amount=float(row['amount']),
                category=row['category'],
                subcategory=row['subcategory'],
                description=row['description'],
                location=row['location'],
                auto_classified=row['auto_classified'],
                account=row['account'],
                currency=row['currency'],
                source=row['source'],
                created_at=row['created_at']
            )
            for row in rows
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")
    finally:
        await pool.close()


@router.get("/monthly/{month}", response_model=MonthlyStats)
async def get_monthly_stats(month: str):
    """
    获取月度统计

    - month格式：YYYY-MM
    - 返回收入、支出、净额、分类占比
    """
    pool = await get_db_pool()

    try:
        # Query monthly totals with currency conversion
        totals_query = """
            WITH base_currency AS (
                SELECT 'CNY' AS currency
            ),
            converted AS (
                SELECT
                    t.date,
                    t.amount,
                    t.currency,
                    -- 如果币种是CNY，汇率为1；否则查询汇率表
                    CASE
                        WHEN t.currency = 'CNY' THEN 1.0
                        ELSE COALESCE(
                            (SELECT fx.rate
                             FROM pams_fx_rates_daily fx
                             WHERE fx.base_currency = 'CNY'
                               AND fx.quote_currency = t.currency
                               AND fx.as_of <= t.date
                             ORDER BY fx.as_of DESC
                             LIMIT 1),
                            1.0  -- 如果没有汇率数据，默认1:1
                        )
                    END AS fx_rate
                FROM pams_transactions t
                WHERE TO_CHAR(t.date, 'YYYY-MM') = $1
            )
            SELECT
                TO_CHAR(date, 'YYYY-MM') AS month,
                COALESCE(SUM(CASE WHEN amount > 0 THEN amount * fx_rate ELSE 0 END), 0) AS total_income,
                COALESCE(SUM(CASE WHEN amount < 0 THEN ABS(amount * fx_rate) ELSE 0 END), 0) AS total_expense,
                COALESCE(SUM(amount * fx_rate), 0) AS net
            FROM converted
            GROUP BY TO_CHAR(date, 'YYYY-MM');
        """

        # Query category breakdown with currency conversion
        category_query = """
            WITH converted AS (
                SELECT
                    t.category,
                    t.amount,
                    CASE
                        WHEN t.currency = 'CNY' THEN 1.0
                        ELSE COALESCE(
                            (SELECT fx.rate
                             FROM pams_fx_rates_daily fx
                             WHERE fx.base_currency = 'CNY'
                               AND fx.quote_currency = t.currency
                               AND fx.as_of <= t.date
                             ORDER BY fx.as_of DESC
                             LIMIT 1),
                            1.0
                        )
                    END AS fx_rate
                FROM pams_transactions t
                WHERE TO_CHAR(t.date, 'YYYY-MM') = $1
                  AND t.amount < 0
            )
            SELECT
                category,
                SUM(ABS(amount * fx_rate)) AS amount,
                COUNT(*) AS count
            FROM converted
            GROUP BY category;
        """

        async with pool.acquire() as conn:
            # Get totals
            totals_row = await conn.fetchrow(totals_query, month)

            # Get category breakdown
            category_rows = await conn.fetch(category_query, month)

        # If no data for this month, return zeros
        if not totals_row or totals_row['month'] is None:
            return MonthlyStats(
                month=month,
                total_income=0.0,
                total_expense=0.0,
                net=0.0,
                by_category=[]
            )

        # Build category breakdown list
        by_category = [
            {
                'category': r['category'],
                'amount': float(r['amount']),
                'count': r['count']
            }
            for r in category_rows
        ]

        return MonthlyStats(
            month=totals_row['month'],
            total_income=float(totals_row['total_income']),
            total_expense=float(totals_row['total_expense']),
            net=float(totals_row['net']),
            by_category=by_category
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")
    finally:
        await pool.close()


@router.get("/daily/{month}")
async def get_daily_stats(month: str):
    """
    获取指定月份的每日统计

    - month格式：YYYY-MM
    - 返回每日的收入、支出数据数组
    """
    pool = await get_db_pool()

    try:
        query = """
            WITH converted AS (
                SELECT
                    t.date,
                    t.amount,
                    CASE
                        WHEN t.currency = 'CNY' THEN 1.0
                        ELSE COALESCE(
                            (SELECT fx.rate
                             FROM pams_fx_rates_daily fx
                             WHERE fx.base_currency = 'CNY'
                               AND fx.quote_currency = t.currency
                               AND fx.as_of <= t.date
                             ORDER BY fx.as_of DESC
                             LIMIT 1),
                            1.0
                        )
                    END AS fx_rate
                FROM pams_transactions t
                WHERE TO_CHAR(t.date, 'YYYY-MM') = $1
            )
            SELECT
                TO_CHAR(date, 'DD') AS day,
                COALESCE(SUM(CASE WHEN amount > 0 THEN amount * fx_rate ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN amount < 0 THEN ABS(amount * fx_rate) ELSE 0 END), 0) AS expense
            FROM converted
            GROUP BY date
            ORDER BY date;
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, month)

        # Convert to list of dicts
        daily_stats = [
            {
                'day': r['day'],
                'income': float(r['income']),
                'expense': float(r['expense'])
            }
            for r in rows
        ]

        return daily_stats

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取每日统计失败: {str(e)}")
    finally:
        await pool.close()


@router.get("/categories-by-range")
async def get_categories_by_range(start: str, end: str):
    """
    获取指定时间段的支出分类

    - start/end格式：YYYY-MM-DD
    - 返回分类占比数组
    """
    pool = await get_db_pool()

    try:
        # 将字符串转换为date对象
        from datetime import datetime as dt
        start_date = dt.strptime(start, '%Y-%m-%d').date()
        end_date = dt.strptime(end, '%Y-%m-%d').date()

        query = """
            WITH converted AS (
                SELECT
                    t.category,
                    t.amount,
                    CASE
                        WHEN t.currency = 'CNY' THEN 1.0
                        ELSE COALESCE(
                            (SELECT fx.rate
                             FROM pams_fx_rates_daily fx
                             WHERE fx.base_currency = 'CNY'
                               AND fx.quote_currency = t.currency
                               AND fx.as_of <= t.date
                             ORDER BY fx.as_of DESC
                             LIMIT 1),
                            1.0
                        )
                    END AS fx_rate
                FROM pams_transactions t
                WHERE t.date >= $1
                  AND t.date <= $2
                  AND t.amount < 0  -- 只统计支出
            )
            SELECT
                category,
                SUM(ABS(amount * fx_rate)) AS amount,
                COUNT(*) AS count
            FROM converted
            GROUP BY category
            ORDER BY amount DESC;
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, start_date, end_date)

        return [
            {
                'category': r['category'],
                'amount': float(r['amount']),
                'count': r['count']
            }
            for r in rows
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取分类数据失败: {str(e)}")
    finally:
        await pool.close()


@router.get("/categories")
async def get_categories():
    """获取所有分类列表"""
    pool = await get_db_pool()

    try:
        query = """
            SELECT DISTINCT category, COUNT(*) AS count
            FROM pams_transactions
            GROUP BY category
            ORDER BY count DESC;
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(query)

        return [{"name": r["category"], "count": r["count"]} for r in rows]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")
    finally:
        await pool.close()


@router.delete("/{transaction_id}")
async def delete_transaction(transaction_id: int):
    """删除交易记录"""
    pool = await get_db_pool()

    try:
        query = "DELETE FROM pams_transactions WHERE id = $1 RETURNING id;"

        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, transaction_id)

        if not row:
            raise HTTPException(status_code=404, detail="交易记录不存在")

        return {"message": "删除成功", "id": row['id']}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")
    finally:
        await pool.close()


@router.get("/budgets/{month}")
async def get_budget_status(month: str):
    """
    获取预算执行情况

    返回各分类的预算、实际支出、进度
    """
    pool = await get_db_pool()

    try:
        query = """
            SELECT
                b.category,
                b.budget_amount,
                b.alert_threshold,
                COALESCE(SUM(ABS(t.amount)) FILTER (WHERE t.amount < 0), 0) AS spent,
                CASE
                    WHEN b.budget_amount > 0 THEN
                        COALESCE(SUM(ABS(t.amount)) FILTER (WHERE t.amount < 0), 0) / b.budget_amount
                    ELSE 0
                END AS progress
            FROM pams_budgets b
            LEFT JOIN pams_transactions t
                ON b.category = t.category
                AND TO_CHAR(t.date, 'YYYY-MM') = TO_CHAR(b.month, 'YYYY-MM')
            WHERE TO_CHAR(b.month, 'YYYY-MM') = $1
            GROUP BY b.category, b.budget_amount, b.alert_threshold;
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, month)

        return [
            {
                "category": r["category"],
                "budget": float(r["budget_amount"]),
                "spent": float(r["spent"]),
                "remaining": float(r["budget_amount"]) - float(r["spent"]),
                "progress": float(r["progress"]),
                "alert": r["progress"] >= r["alert_threshold"]
            }
            for r in rows
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")
    finally:
        await pool.close()


@router.post("/budgets")
async def create_budget(category: str, month: str, amount: float):
    """创建月度预算"""
    pool = await get_db_pool()

    try:
        # 月份格式转换 YYYY-MM -> YYYY-MM-01
        month_date = f"{month}-01"

        query = """
            INSERT INTO pams_budgets (category, month, budget_amount)
            VALUES ($1, $2::date, $3)
            ON CONFLICT (category, month)
            DO UPDATE SET budget_amount = $3
            RETURNING *;
        """

        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, category, month_date, amount)

        return {
            "id": row['id'],
            "category": row['category'],
            "month": row['month'].strftime('%Y-%m'),
            "budget_amount": float(row['budget_amount'])
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建失败: {str(e)}")
    finally:
        await pool.close()
