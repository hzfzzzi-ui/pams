"""
持仓交易 API - 从 Telegram 交易识别提交持仓数据
"""
import os
import logging
from typing import Optional
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
    """Legacy entry: all new imports require the shared draft/preview approval."""
    raise HTTPException(409, detail={"code": "import_review_required",
        "message": "请通过导入资料统一核对后入库", "destination": "account_import.html?kind=holding"})
