"""
盘中 15 分钟刷新模块。
对 PULLBACK_ZONE 和 TRIGGERED 候选进行实时价格检查和状态更新。
升级 TRIGGERED 时自动计算仓位、生成订单草稿并推送 Telegram 通知。
无效化时取消待确认订单。
"""
import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import psycopg2.extras

from .config import (
    get_risk_budget,
    get_satellite_cn_config,
    load_strategy_config,
)
from .indicators import calc_entry_stop
from .risk_sizing import (
    calculate_position_size,
    check_concurrent_risk,
    get_current_risk_state,
)

logger = logging.getLogger(__name__)

# A 股交易时段 (CST = UTC+8)
_TRADING_SESSIONS = [
    (9, 30, 11, 30),   # 上午
    (13, 0, 15, 0),    # 下午
]


def _is_trading_hours() -> bool:
    """检查当前是否在 A 股交易时段内 (CST，仅工作日)。"""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    except ImportError:
        os.environ.setdefault("TZ", "Asia/Shanghai")
        now = datetime.now()

    # 周末不交易 (weekday 5=周六, 6=周日)
    if now.weekday() >= 5:
        return False

    current_minutes = now.hour * 60 + now.minute

    for start_h, start_m, end_h, end_m in _TRADING_SESSIONS:
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= current_minutes <= end:
            return True

    return False


def _extract_code(symbol: str) -> str:
    """从带前缀的 symbol 中提取纯代码 (sh600000 → 600000)。"""
    code = symbol.strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if code.startswith(prefix):
            return code[len(prefix):]
    return code


def _fetch_realtime_batch(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    通过腾讯财经 API 批量获取目标标的实时行情。
    symbols: ['sh600519', 'sz000858', ...] (带 sh/sz 前缀)
    返回: {'600519': {close, open, high, low, volume}, ...} (纯代码为 key)

    单次 HTTP 请求，1 秒内完成，替代 AkShare 全市场下载 (4+ 分钟)。
    """
    if not symbols:
        return {}

    import urllib.request

    url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
    req = urllib.request.Request(url, headers={"User-Agent": "PAMS/2.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("gbk", errors="replace")
    except Exception as e:
        logger.error("腾讯财经实时行情获取失败: %s", e)
        return {}

    result = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        try:
            var_name, value = line.split("=", 1)
            fields = value.strip('"').strip(";").split("~")
            if len(fields) < 35:
                continue
            code = fields[2]  # 纯代码 (600519)
            close = float(fields[3] or 0)
            if close <= 0:
                continue
            result[code] = {
                "close": close,
                "open": float(fields[5] or 0),
                "high": float(fields[33] or 0),
                "low": float(fields[34] or 0),
                "volume": float(fields[6] or 0),
            }
        except (ValueError, IndexError) as e:
            logger.warning("解析实时行情失败 [%s]: %s", line[:50], e)

    logger.info("腾讯实时行情: 请求 %d 只, 获取 %d 只", len(symbols), len(result))
    return result


def get_refresh_targets(conn) -> List[Dict[str, Any]]:
    """
    获取需要盘中刷新的候选标的。
    筛选: 最近扫描中 stage IN ('PULLBACK_ZONE', 'TRIGGERED')。
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, symbol, name, stage, entry_price, stop_price,
                   ma20, ma60, atr14, high_20d, low_20d,
                   quality_score, sent_state, sent_score, scan_date
            FROM pams_strategy_candidates
            WHERE scan_date = (SELECT MAX(scan_date) FROM pams_strategy_candidates)
              AND stage IN ('PULLBACK_ZONE', 'TRIGGERED')
        """)
        return [dict(r) for r in cur.fetchall()]


# ==================== 订单生成 ====================

def _create_intraday_order(
    conn,
    candidate: Dict[str, Any],
    config: Dict[str, Any],
    risk_state: Dict[str, Any],
    rt: Dict[str, Any],
) -> Optional[int]:
    """
    为盘中升级的 TRIGGERED 候选计算仓位并创建订单草稿。

    使用 EOD 存储的 MA20/MA60/ATR14/LOW_20D 计算 entry/stop，
    再基于风险预算计算仓位大小。

    Returns:
        订单 ID 或 None
    """
    satellite = get_satellite_cn_config(config)
    if not satellite:
        return None

    # 用存储指标 + 实时收盘价构造 indicators dict
    indicators = {
        "close": rt["close"],
        "ma20": float(candidate.get("ma20") or 0),
        "ma60": float(candidate.get("ma60") or 0),
        "atr14": float(candidate.get("atr14") or 0),
        "high_20d": float(candidate.get("high_20d") or 0),
        "low_20d": float(candidate.get("low_20d") or 0),
    }

    if indicators["atr14"] <= 0 or indicators["ma20"] <= 0:
        logger.warning("%s: 指标数据不完整, 跳过订单", candidate["symbol"])
        return None

    # 计算 entry/stop
    entry_stop = calc_entry_stop(indicators, satellite)
    entry_price = entry_stop["entry_price"]
    stop_price = entry_stop["stop_price"]

    if entry_price <= 0 or stop_price <= 0 or entry_price <= stop_price:
        logger.warning(
            "%s: entry/stop 无效 (entry=%.2f, stop=%.2f)",
            candidate["symbol"], entry_price, stop_price,
        )
        return None

    # 仓位计算
    risk_budget = get_risk_budget(config, risk_state["risk_mode"])
    sizing = calculate_position_size(
        entry_price, stop_price,
        risk_state["b_nav"],
        risk_budget["per_trade_risk_pct"],
        config,
    )

    if sizing["position_size"] <= 0:
        return None

    # 并发风险检查
    conc_ok, _ = check_concurrent_risk(
        conn, config, sizing["risk_amount_cny"], risk_state["risk_mode"],
    )
    if not conc_ok:
        logger.info("%s: 并发风险超限, 跳过订单", candidate["symbol"])
        return None

    # 更新 candidate 记录 (补全 entry/stop/sizing)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE pams_strategy_candidates
            SET entry_price = %s, stop_price = %s, valid_until = %s,
                position_size = %s, risk_amount_cny = %s, eligible = true
            WHERE id = %s
        """, (
            entry_price, stop_price, entry_stop["valid_until"],
            sizing["position_size"], sizing["risk_amount_cny"],
            candidate["id"],
        ))
        conn.commit()

    # 创建订单草稿
    reason_codes = ["RC_T202_PULLBACK_TRIGGER_OK", "RC_E001_ORDER_DRAFT_CREATED"]
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_order_drafts (
                candidate_id, symbol, market, b_nav, risk_mode,
                entry_price, stop_price, valid_until, position_size,
                position_value_pct, max_loss_cny, risk_amount_cny,
                concurrent_risk_after,
                quality_score, sent_state,
                eligible, hard_veto_hit, reason_codes, status
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s,
                %s, %s,
                %s, %s, %s, 'pending'
            )
            RETURNING id
        """, (
            candidate["id"], candidate["symbol"], "CN_A",
            risk_state["b_nav"], risk_state["risk_mode"],
            entry_price, stop_price, entry_stop["valid_until"],
            sizing["position_size"],
            sizing["position_value_pct"], sizing["max_loss_cny"],
            sizing["risk_amount_cny"],
            risk_state["concurrent_risk_pct"],
            candidate.get("quality_score"), candidate.get("sent_state"),
            True, False,
            json.dumps(reason_codes),
        ))
        row = cur.fetchone()
        conn.commit()
        order_id = row[0] if row else None

    if order_id:
        logger.info(
            "%s: 盘中订单已创建 (id=%d, entry=%.2f, stop=%.2f, %d股)",
            candidate["symbol"], order_id, entry_price, stop_price,
            sizing["position_size"],
        )

    return order_id


def _cancel_invalidated_orders(conn, candidate_id: int) -> int:
    """取消已无效化候选的待确认订单。"""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE pams_order_drafts
            SET status = 'cancelled'
            WHERE candidate_id = %s AND status = 'pending'
        """, (candidate_id,))
        cancelled = cur.rowcount
        conn.commit()
    return cancelled


# ==================== Telegram 通知 ====================

def _send_intraday_notification(conn, changes: List[Dict[str, Any]]) -> None:
    """推送盘中状态变更通知到 Telegram。"""
    chat_id = os.environ.get("OWNER_CHAT_ID")
    if not chat_id:
        return

    upgrades = [c for c in changes if c["new_stage"] == "TRIGGERED"]
    invalids = [c for c in changes if c["new_stage"] == "INVALIDATED"]

    if not upgrades and not invalids:
        return

    lines = ["⚡ 盘中信号更新", ""]

    if upgrades:
        lines.append("🎯 新触发信号:")
        for c in upgrades:
            entry = f"¥{c['entry_price']:,.2f}" if c.get("entry_price") else "—"
            size = c.get("position_size", 0)
            lines.append(
                f"  {c['name']}({c['symbol']}) | 入场 {entry} | {size}股"
            )

    if invalids:
        lines.append("")
        lines.append("❌ 无效化:")
        for c in invalids:
            lines.append(f"  {c['name']}({c['symbol']}) | 跌破MA60")

    body = "\n".join(lines)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_notifications (chat_id, title, body, source, status)
            VALUES (%s, %s, %s, %s, 'pending')
        """, (int(chat_id), "盘中信号更新", body, "intraday"))
        conn.commit()

    logger.info("盘中 Telegram 通知已创建")


# ==================== 逐标的刷新 ====================

def refresh_single(
    conn,
    candidate: Dict[str, Any],
    rt: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    risk_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    刷新单个候选的盘中状态。

    PULLBACK_ZONE → 检查 trigger_day → 升级 TRIGGERED + 生成订单
    TRIGGERED → 检查跌破 MA60 → INVALIDATED + 取消订单
    """
    symbol = candidate["symbol"]
    name = candidate.get("name", "")
    stage = candidate["stage"]

    if not rt or rt.get("close", 0) <= 0:
        return {
            "symbol": symbol, "name": name,
            "old_stage": stage, "new_stage": stage, "changed": False,
        }

    new_stage = stage
    close = rt["close"]
    open_p = rt["open"]
    ma20 = float(candidate.get("ma20") or 0)
    order_id = None
    entry_price = None
    position_size = None
    cancelled = 0

    if stage == "PULLBACK_ZONE":
        # trigger_day: close > MA20 且 close > open
        if close > ma20 and close > open_p and ma20 > 0:
            new_stage = "TRIGGERED"

    elif stage == "TRIGGERED":
        # 跌破 MA60 → 无效化
        ma60 = float(candidate.get("ma60") or 0)
        low = rt.get("low", close)
        if ma60 > 0 and low < ma60:
            new_stage = "INVALIDATED"

    changed = new_stage != stage

    if not changed:
        return {
            "symbol": symbol, "name": name,
            "old_stage": stage, "new_stage": stage,
            "changed": False, "close": close,
        }

    # 更新 stage
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE pams_strategy_candidates
            SET stage = %s
            WHERE id = %s
        """, (new_stage, candidate["id"]))
        conn.commit()

    logger.info(
        "%s: 盘中状态变更 %s → %s (close=%.2f, ma20=%.2f)",
        symbol, stage, new_stage, close, ma20,
    )

    # 升级 → 生成订单
    if new_stage == "TRIGGERED" and config and risk_state:
        if risk_state["can_open_new"]:
            order_id = _create_intraday_order(
                conn, candidate, config, risk_state, rt,
            )
            if order_id:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT entry_price, position_size FROM pams_strategy_candidates WHERE id = %s",
                        (candidate["id"],),
                    )
                    updated = cur.fetchone()
                if updated:
                    entry_price = float(updated["entry_price"] or 0)
                    position_size = int(updated["position_size"] or 0)

    # 无效化 → 取消待确认订单
    if new_stage == "INVALIDATED":
        cancelled = _cancel_invalidated_orders(conn, candidate["id"])
        if cancelled:
            logger.info("%s: 取消 %d 笔待确认订单", symbol, cancelled)

    return {
        "symbol": symbol,
        "name": name,
        "old_stage": stage,
        "new_stage": new_stage,
        "changed": True,
        "close": close,
        "order_id": order_id,
        "entry_price": entry_price,
        "position_size": position_size,
        "cancelled_orders": cancelled,
    }


# ==================== 主入口 ====================

def run_intraday_refresh(conn) -> Dict[str, Any]:
    """
    盘中刷新主入口。

    1. 检查是否在交易时段
    2. 一次性拉取全市场实时行情
    3. 加载配置 + 风控状态
    4. 逐标的刷新 (状态升级/无效化)
    5. 推送 Telegram 通知
    """
    if not _is_trading_hours():
        return {
            "refreshed": 0,
            "upgraded": 0,
            "invalidated": 0,
            "orders_created": 0,
            "skipped_non_trading": True,
        }

    targets = get_refresh_targets(conn)
    if not targets:
        return {"refreshed": 0, "upgraded": 0, "invalidated": 0, "orders_created": 0}

    # 构造腾讯 API 需要的 symbol 列表 (sh600519, sz000858 等)
    tencent_symbols = [c["symbol"] for c in targets if c.get("symbol")]

    # 批量获取实时行情 (1 次 HTTP 请求, ~1 秒)
    rt_all = _fetch_realtime_batch(tencent_symbols)
    if not rt_all:
        logger.warning("实时行情获取失败, 跳过本轮刷新")
        return {
            "refreshed": 0, "upgraded": 0, "invalidated": 0,
            "orders_created": 0, "error": "实时行情获取失败",
        }

    # 加载配置 + 风控状态 (用于订单生成)
    config = load_strategy_config(conn)
    risk_state = get_current_risk_state(conn, config) if config else None

    upgraded = 0
    invalidated = 0
    orders_created = 0
    changes: List[Dict[str, Any]] = []

    for candidate in targets:
        code = _extract_code(candidate["symbol"])
        rt = rt_all.get(code, {})

        result = refresh_single(conn, candidate, rt, config, risk_state)
        if result["changed"]:
            changes.append(result)
            if result["new_stage"] == "TRIGGERED":
                upgraded += 1
                if result.get("order_id"):
                    orders_created += 1
            elif result["new_stage"] == "INVALIDATED":
                invalidated += 1

    # Telegram 通知
    if changes:
        try:
            _send_intraday_notification(conn, changes)
        except Exception as e:
            logger.error("盘中通知推送失败: %s", e)

    logger.info(
        "盘中刷新完成: 目标=%d, 升级=%d, 无效=%d, 订单=%d",
        len(targets), upgraded, invalidated, orders_created,
    )

    return {
        "refreshed": len(targets),
        "upgraded": upgraded,
        "invalidated": invalidated,
        "orders_created": orders_created,
        "changes": [
            {
                "symbol": c["symbol"],
                "name": c["name"],
                "old_stage": c["old_stage"],
                "new_stage": c["new_stage"],
            }
            for c in changes
        ],
    }
