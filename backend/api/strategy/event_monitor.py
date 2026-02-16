"""
事件监控模块。
检查停牌状态、爬取公告事件、判断是否触发事件否决。
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras

logger = logging.getLogger(__name__)

# 事件关键词 → (event_type, severity, reason_code)
_EVENT_KEYWORDS = [
    (["预亏", "预减", "业绩预警", "业绩大幅下降"], "preloss_warning", "HIGH", "RC_F201_EVENT_PRELOSS"),
    (["立案", "调查", "处罚", "违规", "违法"], "investigation", "CRITICAL", "RC_F202_EVENT_INVESTIGATION"),
    (["停牌", "异常停牌", "暂停上市"], "abnormal_halt", "HIGH", "RC_F203_EVENT_ABNORMAL_HALT"),
]


def _to_akshare_code(symbol: str) -> str:
    """将 'sh600519' / 'sz000001' 转为 '600519' / '000001'。"""
    s = symbol.strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


# ==================== 停牌检查 ====================

def check_suspension(symbol: str) -> Tuple[bool, str]:
    """
    检查标的是否停牌。

    通过 AkShare 实时行情判断成交量/成交额是否为 0。

    Returns:
        (is_suspended, reason)
    """
    try:
        import akshare as ak

        code = _to_akshare_code(symbol)
        df = ak.stock_zh_a_spot_em()

        if df is None or df.empty:
            return False, ""

        match = df[df["代码"] == code]
        if match.empty:
            return False, ""

        row = match.iloc[0]
        volume = float(row.get("成交量", 1) or 1)
        turnover = float(row.get("成交额", 1) or 1)

        if volume == 0 or turnover == 0:
            return True, f"{symbol} 停牌 (成交量/额为 0)"

        return False, ""

    except Exception as e:
        logger.warning("%s: 停牌检查失败: %s", symbol, e)
        return False, ""


# ==================== 事件爬取 ====================

def crawl_events(symbol: str, days_back: int = 7) -> List[Dict[str, Any]]:
    """
    从 AkShare 获取标的最近公告，按关键词分类事件。

    Returns:
        List[{symbol, event_type, event_date, severity, title, source, reason_code}]
    """
    events = []
    code = _to_akshare_code(symbol)

    try:
        import akshare as ak

        df = ak.stock_notice_report(symbol=code)

        if df is None or df.empty:
            return events

        cutoff = (date.today() - timedelta(days=days_back)).isoformat()

        for _, row in df.head(20).iterrows():
            title = str(row.get("公告标题", "") or "")
            pub_date = str(row.get("公告日期", "") or "")[:10]

            if pub_date < cutoff:
                continue

            for keywords, event_type, severity, reason_code in _EVENT_KEYWORDS:
                if any(kw in title for kw in keywords):
                    events.append({
                        "symbol": symbol,
                        "event_type": event_type,
                        "event_date": pub_date or date.today().isoformat(),
                        "severity": severity,
                        "title": title[:200],
                        "source": "akshare_notice",
                        "reason_code": reason_code,
                    })
                    break

    except Exception as e:
        logger.warning("%s: 公告爬取失败: %s", symbol, e)

    return events


# ==================== 事件存储 ====================

def store_events(conn, events: List[Dict[str, Any]]) -> int:
    """
    将事件写入 pams_event_monitor。

    Returns:
        入库数量
    """
    if not events:
        return 0

    count = 0
    with conn.cursor() as cur:
        for ev in events:
            cur.execute("""
                INSERT INTO pams_event_monitor
                    (symbol, market, event_type, event_date, severity, title, source, is_active)
                VALUES (%s, 'CN_A', %s, %s, %s, %s, %s, true)
                ON CONFLICT DO NOTHING
            """, (
                ev["symbol"], ev["event_type"], ev["event_date"],
                ev["severity"], ev.get("title"), ev.get("source"),
            ))
            count += 1
        conn.commit()

    return count


# ==================== 事件否决检查 ====================

def check_event_veto(
    conn,
    symbol: str,
    days_back: int = 1,
) -> Tuple[bool, List[str]]:
    """
    检查标的近期是否有重大负面事件。

    查询 pams_event_monitor 中 severity = 'CRITICAL' 或 'HIGH' 的近期事件，
    以及实时爬取最新公告。

    Returns:
        (veto_hit, reason_codes)
    """
    reason_codes: List[str] = []

    # 1. 先爬取最新公告入库
    events = crawl_events(symbol, days_back=max(days_back, 3))
    if events:
        store_events(conn, events)
        for ev in events:
            rc = ev.get("reason_code")
            if rc and rc not in reason_codes:
                reason_codes.append(rc)

    # 2. 查询 DB 中 HIGH/CRITICAL 级别的近期事件
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT event_type
            FROM pams_event_monitor
            WHERE symbol = %s
              AND market = 'CN_A'
              AND is_active = true
              AND event_date >= %s
              AND severity IN ('CRITICAL', 'HIGH')
        """, (symbol, cutoff))
        rows = cur.fetchall()

    db_reason_map = {
        "preloss_warning": "RC_F201_EVENT_PRELOSS",
        "investigation": "RC_F202_EVENT_INVESTIGATION",
        "abnormal_halt": "RC_F203_EVENT_ABNORMAL_HALT",
    }

    for row in rows:
        rc = db_reason_map.get(row["event_type"])
        if rc and rc not in reason_codes:
            reason_codes.append(rc)

    veto_hit = len(reason_codes) > 0
    return veto_hit, reason_codes


# ==================== 批量刷新 ====================

def refresh_events_batch(conn, symbols: List[str]) -> Dict[str, Any]:
    """
    批量刷新多个标的的事件监控。

    Returns:
        {symbols_checked, events_found, events_stored}
    """
    total_events = 0
    total_stored = 0

    for symbol in symbols:
        events = crawl_events(symbol, days_back=7)
        total_events += len(events)
        if events:
            stored = store_events(conn, events)
            total_stored += stored

    return {
        "symbols_checked": len(symbols),
        "events_found": total_events,
        "events_stored": total_stored,
    }
