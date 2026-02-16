"""
市场情绪引擎。
基于市场宽度指标（涨跌家数、涨停跌停、指数大阴线频率）判定 HOT/NEUTRAL/PANIC。
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

import psycopg2.extras

logger = logging.getLogger(__name__)


# ==================== 市场宽度数据 ====================

def fetch_market_breadth() -> Dict[str, Any]:
    """
    从 AkShare 获取 A 股市场宽度数据。

    Returns:
        {advancers, decliners, limit_up, limit_down, total}
        获取失败返回默认中性值。
    """
    result = {
        "advancers": 0,
        "decliners": 0,
        "limit_up": 0,
        "limit_down": 0,
        "total": 0,
    }

    try:
        import akshare as ak

        # A 股实时行情 → 计算涨跌家数
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            result["total"] = len(df)
            changes = df["涨跌幅"].astype(float)
            result["advancers"] = int((changes > 0).sum())
            result["decliners"] = int((changes < 0).sum())

        # 涨停池
        try:
            today_str = date.today().strftime("%Y%m%d")
            zt_df = ak.stock_zt_pool_em(date=today_str)
            result["limit_up"] = len(zt_df) if zt_df is not None else 0
        except Exception as e:
            logger.warning("涨停池获取失败: %s", e)

        # 跌停池
        try:
            dt_df = ak.stock_dt_pool_em(date=today_str)
            result["limit_down"] = len(dt_df) if dt_df is not None else 0
        except Exception as e:
            logger.warning("跌停池获取失败: %s", e)

    except Exception as e:
        logger.error("市场宽度获取失败: %s", e)

    return result


def calc_big_red_freq(conn, days: int = 7) -> int:
    """
    计算最近 N 天内沪深300 大阴线次数。

    大阴线定义: 日涨跌幅 < -2%。
    从 pams_ohlcv_daily 中查询 sh000300。

    Returns:
        大阴线天数
    """
    cutoff = (date.today() - timedelta(days=days + 5)).isoformat()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM (
                SELECT close, LAG(close) OVER (ORDER BY trade_date) AS prev_close
                FROM pams_ohlcv_daily
                WHERE symbol = 'sh000300' AND market = 'CN_A'
                  AND trade_date >= %s
                ORDER BY trade_date DESC
                LIMIT %s
            ) sub
            WHERE prev_close > 0
              AND (close - prev_close) / prev_close < -0.02
        """, (cutoff, days + 1))
        row = cur.fetchone()

    return int(row["cnt"]) if row else 0


# ==================== 情绪判定 ====================

def determine_sent_state(
    breadth: Dict[str, Any],
    big_red_count: int,
) -> str:
    """
    基于市场宽度和大阴线频率判定情绪状态。

    规则:
        PANIC: decliners/advancers > 2.0 且 limit_down > 50
               OR big_red_7d >= 3
               OR limit_down > 100
        HOT:   advancers/decliners > 2.5 且 limit_up > 100
               OR limit_up > 200
        默认:  NEUTRAL

    Returns:
        "HOT" | "NEUTRAL" | "PANIC"
    """
    advancers = breadth.get("advancers", 0)
    decliners = breadth.get("decliners", 0)
    limit_up = breadth.get("limit_up", 0)
    limit_down = breadth.get("limit_down", 0)

    ad_ratio = decliners / max(advancers, 1)
    da_ratio = advancers / max(decliners, 1)

    # PANIC 检查
    if (ad_ratio > 2.0 and limit_down > 50) or big_red_count >= 3 or limit_down > 100:
        return "PANIC"

    # HOT 检查
    if (da_ratio > 2.5 and limit_up > 100) or limit_up > 200:
        return "HOT"

    return "NEUTRAL"


def get_sent_score(
    sent_state: str,
    satellite_config: Optional[Dict] = None,
) -> float:
    """
    将情绪状态映射到评分。

    默认: HOT=62, NEUTRAL=70, PANIC=50
    可通过 config 覆盖。

    Returns:
        float 评分
    """
    default_map = {"HOT": 62.0, "NEUTRAL": 70.0, "PANIC": 50.0}

    if satellite_config:
        score_map = (
            satellite_config
            .get("sentiment", {})
            .get("scoring", {})
            .get("sent_score_map", default_map)
        )
    else:
        score_map = default_map

    return float(score_map.get(sent_state, 70.0))


# ==================== 刷新入口 ====================

def refresh_sentiment(conn) -> Dict[str, Any]:
    """
    获取市场宽度 → 计算大阴线 → 判定情绪 → 写入 DB。

    Returns:
        {as_of, sent_state, sent_score, advancers, decliners, limit_up, limit_down, big_red_7d}
    """
    today = date.today().isoformat()

    breadth = fetch_market_breadth()
    big_red_count = calc_big_red_freq(conn, days=7)
    sent_state = determine_sent_state(breadth, big_red_count)
    sent_score = get_sent_score(sent_state)

    snapshot = {
        "as_of": today,
        "sent_state": sent_state,
        "sent_score": sent_score,
        "advancers": breadth.get("advancers", 0),
        "decliners": breadth.get("decliners", 0),
        "limit_up": breadth.get("limit_up", 0),
        "limit_down": breadth.get("limit_down", 0),
        "big_red_7d": big_red_count,
    }

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_sentiment_state
                (as_of, sent_state, sent_score, advancers, decliners,
                 limit_up, limit_down, big_red_7d)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (as_of) DO UPDATE SET
                sent_state = EXCLUDED.sent_state,
                sent_score = EXCLUDED.sent_score,
                advancers = EXCLUDED.advancers,
                decliners = EXCLUDED.decliners,
                limit_up = EXCLUDED.limit_up,
                limit_down = EXCLUDED.limit_down,
                big_red_7d = EXCLUDED.big_red_7d
        """, (
            today, sent_state, sent_score,
            breadth.get("advancers", 0), breadth.get("decliners", 0),
            breadth.get("limit_up", 0), breadth.get("limit_down", 0),
            big_red_count,
        ))
        conn.commit()

    logger.info(
        "情绪刷新: state=%s, score=%.1f, adv=%d, dec=%d, up=%d, down=%d, big_red=%d",
        sent_state, sent_score,
        breadth.get("advancers", 0), breadth.get("decliners", 0),
        breadth.get("limit_up", 0), breadth.get("limit_down", 0),
        big_red_count,
    )

    return snapshot
