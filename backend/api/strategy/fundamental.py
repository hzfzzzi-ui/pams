"""
基本面门控模块。
通过 AkShare 获取财务指标，计算质量评分，检查现金流趋势和行业景气度。
"""
import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras

logger = logging.getLogger(__name__)

# AkShare symbol 格式 → 纯数字 6 位
def _to_akshare_code(symbol: str) -> str:
    """将 'sh600519' / 'sz000001' 转为 '600519' / '000001'。"""
    s = symbol.strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


# ==================== 数据获取 ====================

def fetch_fundamental_akshare(symbol: str) -> Dict[str, Any]:
    """
    从 AkShare 获取最新财务分析指标。

    Returns:
        {roe, gross_margin, debt_ratio, op_cashflow_ttm, net_profit_ttm}
        获取失败时返回空 dict。
    """
    try:
        import akshare as ak

        code = _to_akshare_code(symbol)

        # 财务分析指标 (ROE、毛利率、资产负债率)
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2023")
        if df is None or df.empty:
            logger.warning("%s: 财务分析指标为空", symbol)
            return {}

        # 取最新一行
        latest = df.iloc[0]

        roe = _safe_float(latest.get("净资产收益率(%)"))
        gross_margin = _safe_float(latest.get("销售毛利率(%)"))
        debt_ratio = _safe_float(latest.get("资产负债率(%)"))

        time.sleep(0.3)

        # 现金流量表
        result = {
            "roe": roe,
            "gross_margin": gross_margin,
            "debt_ratio": debt_ratio,
            "op_cashflow_ttm": None,
            "net_profit_ttm": None,
        }

        try:
            cf_df = ak.stock_cash_flow_sheet_by_report_em(symbol=code)
            if cf_df is not None and not cf_df.empty:
                cf_latest = cf_df.iloc[0]
                result["op_cashflow_ttm"] = _safe_float(
                    cf_latest.get("经营活动产生的现金流量净额")
                )
                result["net_profit_ttm"] = _safe_float(
                    cf_latest.get("净利润")
                )
        except Exception as e:
            logger.warning("%s: 现金流量表获取失败: %s", symbol, e)

        return result

    except Exception as e:
        logger.error("%s: 基本面数据获取失败: %s", symbol, e)
        return {}


def _safe_float(val: Any) -> Optional[float]:
    """安全转换为 float，失败返回 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ==================== 质量评分 ====================

def calculate_quality_score(data: Dict[str, Any]) -> float:
    """
    计算基本面质量评分 (0–100)。

    评分公式:
        ROE:        > 15% → 30,  > 10% → 20,  > 5% → 10,  else 0
        毛利率:     > 40% → 20,  > 25% → 15,  > 15% → 10,  else 0
        负债率:     < 30% → 25,  < 50% → 20,  < 70% → 10,  else 0
        经营现金流:  > 0 且 > 净利润×0.5 → 25,  > 0 → 15,  else 0
    """
    score = 0.0

    roe = data.get("roe")
    if roe is not None:
        if roe > 15:
            score += 30
        elif roe > 10:
            score += 20
        elif roe > 5:
            score += 10

    gross_margin = data.get("gross_margin")
    if gross_margin is not None:
        if gross_margin > 40:
            score += 20
        elif gross_margin > 25:
            score += 15
        elif gross_margin > 15:
            score += 10

    debt_ratio = data.get("debt_ratio")
    if debt_ratio is not None:
        if debt_ratio < 30:
            score += 25
        elif debt_ratio < 50:
            score += 20
        elif debt_ratio < 70:
            score += 10

    op_cf = data.get("op_cashflow_ttm")
    net_profit = data.get("net_profit_ttm")
    if op_cf is not None:
        if op_cf > 0:
            if net_profit and op_cf > net_profit * 0.5:
                score += 25
            else:
                score += 15

    return round(score, 1)


# ==================== 现金流趋势 ====================

def check_cashflow_trend(
    conn,
    symbol: str,
    quarters: int = 8,
) -> Tuple[bool, List[str]]:
    """
    检查最近 N 个季度经营现金流是否持续恶化。

    恶化定义: 连续 3 个季度环比下降。

    Returns:
        (deterioration_found, reason_codes)
    """
    try:
        import akshare as ak

        code = _to_akshare_code(symbol)
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=code)

        if df is None or df.empty:
            return False, []

        # 取最近 quarters 行
        rows = df.head(quarters)
        cashflows = []
        for _, row in rows.iterrows():
            val = _safe_float(row.get("经营活动产生的现金流量净额"))
            if val is not None:
                cashflows.append(val)

        if len(cashflows) < 4:
            return False, []

        # 检查连续 3 个季度环比下降
        consecutive_decline = 0
        for i in range(len(cashflows) - 1):
            if cashflows[i] < cashflows[i + 1]:
                consecutive_decline += 1
                if consecutive_decline >= 3:
                    return True, ["RC_F101_CASHFLOW_DETERIORATE"]
            else:
                consecutive_decline = 0

        return False, []

    except Exception as e:
        logger.warning("%s: 现金流趋势检查失败: %s", symbol, e)
        return False, []


# ==================== 行业趋势 ====================

def check_industry_regime(
    conn,
    symbol: str,
) -> Tuple[bool, List[str]]:
    """
    简版行业趋势检查。

    从 pams_ohlcv_daily 获取标的行业指数代理的 MA20/MA60，
    若 MA20 < MA60 视为行业下行趋势。

    当前简版: 无行业映射表，返回 (False, [])，不阻断。
    后续可通过 AkShare 行业分类接口补全。
    """
    # TODO: 实现行业指数代理检查
    # 1. 从 symbol → industry_code 映射
    # 2. 获取行业指数 OHLCV
    # 3. 计算 MA20/MA60，判断趋势
    return False, []


# ==================== 刷新入口 ====================

def refresh_fundamental(conn, symbol: str) -> Dict[str, Any]:
    """
    获取基本面数据 → 计算质量评分 → 检查现金流 → 写入 DB。

    Returns:
        {symbol, quality_score, cashflow_deterioration, ...}
    """
    today = date.today().isoformat()
    data = fetch_fundamental_akshare(symbol)

    if not data:
        return {
            "symbol": symbol,
            "quality_score": 50.0,
            "cashflow_deterioration": False,
            "reason_codes": [],
        }

    quality_score = calculate_quality_score(data)
    reason_codes = []

    if quality_score >= 70:
        reason_codes.append("RC_F001_QUALITY_SCORE_HIGH")

    # 现金流趋势 (复用已获取数据避免重复 API 调用)
    cf_deterioration, cf_reasons = check_cashflow_trend(conn, symbol)
    reason_codes = [*reason_codes, *cf_reasons]

    # 行业趋势
    ind_downtrend, ind_reasons = check_industry_regime(conn, symbol)
    reason_codes = [*reason_codes, *ind_reasons]

    # 写入 pams_fundamental_snapshot
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_fundamental_snapshot (
                symbol, market, as_of,
                roe, gross_margin, debt_ratio,
                op_cashflow_ttm, net_profit_ttm,
                quality_score, cashflow_deterioration,
                industry_code, industry_trend
            ) VALUES (
                %s, 'CN_A', %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s
            )
            ON CONFLICT (symbol, market, as_of) DO UPDATE SET
                roe = EXCLUDED.roe,
                gross_margin = EXCLUDED.gross_margin,
                debt_ratio = EXCLUDED.debt_ratio,
                op_cashflow_ttm = EXCLUDED.op_cashflow_ttm,
                net_profit_ttm = EXCLUDED.net_profit_ttm,
                quality_score = EXCLUDED.quality_score,
                cashflow_deterioration = EXCLUDED.cashflow_deterioration
        """, (
            symbol, today,
            data.get("roe"), data.get("gross_margin"), data.get("debt_ratio"),
            data.get("op_cashflow_ttm"), data.get("net_profit_ttm"),
            quality_score, cf_deterioration,
            None, None,
        ))
        conn.commit()

    logger.info(
        "%s: 基本面刷新完成 — quality=%.1f, cf_deterioration=%s",
        symbol, quality_score, cf_deterioration,
    )

    return {
        "symbol": symbol,
        "quality_score": quality_score,
        "cashflow_deterioration": cf_deterioration,
        "roe": data.get("roe"),
        "gross_margin": data.get("gross_margin"),
        "debt_ratio": data.get("debt_ratio"),
        "reason_codes": reason_codes,
    }
