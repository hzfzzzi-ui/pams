"""
持仓交易 API - 从 Telegram 交易识别提交持仓数据
"""
import os
import logging
from typing import Optional
from datetime import date as Date
from decimal import Decimal

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)

# 账户 → (大类, 小类, 币种) 映射
# minor_category 必须匹配 pams_asset_categories 表中的 FK
ACCOUNT_CATEGORY_MAP = {
    'tonghuashun':       ('A股',   'A股',        'CNY'),
    'guangfa':           ('A股',   'A股',        'CNY'),
    'ibkr':              ('美股',  '美股基金',   'USD'),
    'binance':           ('加密',  'ETH',        'USD'),
    'tencent_licaitong': ('理财',  '固收理财',   'CNY'),
    'jianhang':          ('理财',  '固收理财',   'CNY'),
    'wechat':            ('现金',  '现金/活期',  'CNY'),
    'huawei':            ('现金',  '现金/活期',  'CNY'),
    'gongjijin':         ('公积金', '现金/活期',  'CNY'),
    'santander':         ('现金',  '现金/活期',  'MXN'),
    'jiekuan':           ('借款',  '借款',       'CNY'),
}


def get_conn():
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
    if isinstance(v, Decimal):
        return float(v)
    return v


class TradeUpsert(BaseModel):
    account_code: str
    symbol: Optional[str] = None
    name: str
    quantity: float
    buy_price: Optional[float] = None
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    currency: Optional[str] = None
    major_category: Optional[str] = None
    minor_category: Optional[str] = None
    asset_liability: str = "资产"
    as_of: Optional[str] = None


@router.post("/holdings/trade")
def upsert_trade(trade: TradeUpsert):
    """从交易识别结果 UPSERT 到 pams_holdings_snapshot"""

    # 验证 account_code
    defaults = ACCOUNT_CATEGORY_MAP.get(trade.account_code)
    if not defaults:
        raise HTTPException(400, f"未知账户: {trade.account_code}")

    default_major, default_minor, default_currency = defaults

    # 推断缺省字段
    currency = trade.currency or default_currency
    major_category = trade.major_category or default_major
    minor_category = trade.minor_category or default_minor
    as_of = trade.as_of or Date.today().isoformat()

    # 计算 market_value 和 cost
    market_value = trade.market_value
    if market_value is None and trade.current_price is not None:
        market_value = trade.quantity * trade.current_price

    cost = None
    if trade.buy_price is not None:
        cost = trade.quantity * trade.buy_price

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO pams_holdings_snapshot (
                    symbol, name, asset_liability, currency, account_code,
                    major_category, minor_category, market_value, cost,
                    quantity, buy_price, current_price, as_of
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (account_code, name, as_of)
                DO UPDATE SET
                    symbol = COALESCE(EXCLUDED.symbol, pams_holdings_snapshot.symbol),
                    quantity = EXCLUDED.quantity,
                    buy_price = COALESCE(EXCLUDED.buy_price, pams_holdings_snapshot.buy_price),
                    current_price = COALESCE(EXCLUDED.current_price, pams_holdings_snapshot.current_price),
                    market_value = EXCLUDED.market_value,
                    cost = COALESCE(EXCLUDED.cost, pams_holdings_snapshot.cost),
                    major_category = EXCLUDED.major_category,
                    minor_category = EXCLUDED.minor_category,
                    currency = EXCLUDED.currency,
                    updated_at = NOW()
                RETURNING *
            """, (
                trade.symbol, trade.name, trade.asset_liability, currency,
                trade.account_code, major_category, minor_category,
                market_value, cost, trade.quantity, trade.buy_price,
                trade.current_price, as_of,
            ))
            row = cur.fetchone()
        conn.commit()

        result = {k: _norm(v) for k, v in row.items()} if row else {}
        logger.info("Trade upserted: %s %s @ %s", trade.account_code, trade.name, as_of)
        return {"success": True, "message": "持仓已更新", "holding": result}

    except Exception as e:
        conn.rollback()
        logger.error("Trade upsert failed: %s", e)
        raise HTTPException(500, f"持仓更新失败: {e}")
    finally:
        conn.close()
