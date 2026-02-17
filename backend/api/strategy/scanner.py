"""
EOD 扫描主编排模块。
串联 Universe → 技术扫描 → 量价确认 → 仓位计算 → 订单草稿生成 → Telegram 推送。
"""
import json
import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

import psycopg2.extras

from .config import (
    add_reason,
    add_reasons,
    get_b_nav,
    get_risk_budget,
    get_satellite_cn_config,
    load_strategy_config,
)
from .data_feed import (
    fetch_and_store_batch,
    fetch_index_ohlcv_akshare,
    get_index_ohlcv_from_db,
    get_ohlcv_from_db,
    store_ohlcv,
)
from .flow_confirm import compute_flow_confirm
from .indicators import (
    calc_entry_stop,
    check_pullback_zone,
    check_trend_gate,
    check_trigger_day,
    compute_indicators,
)
from .cooldown import check_cooldown
from .event_monitor import check_event_veto, check_suspension
from .fundamental import refresh_fundamental
from .hard_veto import check_hard_veto
from .sentiment import check_symbol_heat, fetch_social_heat, get_sent_score, refresh_sentiment
from .risk_sizing import (
    calculate_position_size,
    check_concurrent_risk,
    check_theme_exposure,
    get_current_risk_state,
)
from .universe_filter import (
    filter_blacklist,
    filter_liquidity,
    filter_st_delist,
    get_universe_symbols,
)

logger = logging.getLogger(__name__)


def _get_symbol_name(conn, symbol: str) -> str:
    """从持仓 → 观察池 → AkShare 逐级获取标的名称。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 1. 持仓快照
        cur.execute("""
            SELECT name FROM pams_holdings_snapshot
            WHERE symbol = %s AND name IS NOT NULL AND name != ''
            ORDER BY as_of DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
        if row:
            return row["name"]

        # 2. 观察池
        cur.execute("""
            SELECT name FROM pams_watchlist
            WHERE symbol = %s AND name IS NOT NULL AND name != ''
            LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
        if row:
            return row["name"]

    # 3. AkShare 兜底
    try:
        import akshare as ak
        s = symbol.strip().lower()
        code = s
        for prefix in ("sh", "sz", "bj"):
            if s.startswith(prefix):
                code = s[len(prefix):]
                break
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty:
            name_row = df[df["item"] == "股票简称"]
            if not name_row.empty:
                return str(name_row.iloc[0]["value"])
    except Exception as e:
        logger.debug("%s: AkShare 名称获取失败: %s", symbol, e)

    return symbol


def _get_cached_quality_score(conn, symbol: str) -> Optional[float]:
    """从 pams_fundamental_snapshot 读取缓存的质量评分（不调 API）。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT quality_score FROM pams_fundamental_snapshot
            WHERE symbol = %s
            ORDER BY as_of DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
    return float(row["quality_score"]) if row and row["quality_score"] is not None else None


def _save_candidate(conn, candidate: Dict[str, Any]) -> int:
    """保存候选到 pams_strategy_candidates，返回 id。"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_strategy_candidates (
                symbol, name, market, scan_date, stage,
                close, ma20, ma60, atr14, high_20d, low_20d, max_return_40d,
                flow_result, flow_score, confirm_score,
                quality_score, sent_state, sent_score,
                eligible, hard_veto_hit, reason_codes,
                entry_price, stop_price, valid_until, position_size, risk_amount_cny
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            ON CONFLICT (symbol, market, scan_date) DO UPDATE SET
                stage = EXCLUDED.stage,
                close = EXCLUDED.close,
                ma20 = EXCLUDED.ma20, ma60 = EXCLUDED.ma60,
                atr14 = EXCLUDED.atr14,
                high_20d = EXCLUDED.high_20d, low_20d = EXCLUDED.low_20d,
                max_return_40d = EXCLUDED.max_return_40d,
                flow_result = EXCLUDED.flow_result, flow_score = EXCLUDED.flow_score,
                confirm_score = EXCLUDED.confirm_score,
                quality_score = EXCLUDED.quality_score,
                sent_state = EXCLUDED.sent_state, sent_score = EXCLUDED.sent_score,
                eligible = EXCLUDED.eligible, hard_veto_hit = EXCLUDED.hard_veto_hit,
                reason_codes = EXCLUDED.reason_codes,
                entry_price = EXCLUDED.entry_price, stop_price = EXCLUDED.stop_price,
                valid_until = EXCLUDED.valid_until,
                position_size = EXCLUDED.position_size,
                risk_amount_cny = EXCLUDED.risk_amount_cny
            RETURNING id
        """, (
            candidate["symbol"], candidate["name"], candidate["market"],
            candidate["scan_date"], candidate["stage"],
            candidate.get("close"), candidate.get("ma20"), candidate.get("ma60"),
            candidate.get("atr14"), candidate.get("high_20d"), candidate.get("low_20d"),
            candidate.get("max_return_40d"),
            candidate.get("flow_result"), candidate.get("flow_score"),
            candidate.get("confirm_score"),
            candidate.get("quality_score"), candidate.get("sent_state"),
            candidate.get("sent_score"),
            candidate.get("eligible", True), candidate.get("hard_veto_hit", False),
            json.dumps(candidate.get("reason_codes", [])),
            candidate.get("entry_price"), candidate.get("stop_price"),
            candidate.get("valid_until"), candidate.get("position_size"),
            candidate.get("risk_amount_cny"),
        ))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0


def _save_order_draft(conn, candidate: Dict[str, Any], candidate_id: int, risk_state: Dict) -> int:
    """保存订单草稿到 pams_order_drafts，返回 id。"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_order_drafts (
                candidate_id, symbol, market, b_nav, risk_mode,
                entry_price, stop_price, valid_until, position_size,
                position_value_pct, max_loss_cny, risk_amount_cny,
                concurrent_risk_after,
                flow_confirm, confirm_score,
                quality_score, sent_state,
                eligible, hard_veto_hit, reason_codes, status
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s,
                %s, %s,
                %s, %s,
                %s, %s, %s, 'pending'
            )
            RETURNING id
        """, (
            candidate_id, candidate["symbol"], candidate["market"],
            risk_state["b_nav"], risk_state["risk_mode"],
            candidate["entry_price"], candidate["stop_price"],
            candidate["valid_until"], candidate["position_size"],
            candidate.get("position_value_pct"), candidate.get("max_loss_cny"),
            candidate.get("risk_amount_cny"),
            risk_state["concurrent_risk_pct"],
            candidate.get("flow_result"), candidate.get("confirm_score"),
            candidate.get("quality_score"), candidate.get("sent_state"),
            candidate.get("eligible", True), candidate.get("hard_veto_hit", False),
            json.dumps(candidate.get("reason_codes", [])),
        ))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0


def _send_notification(conn, summary: Dict[str, Any]) -> None:
    """通过 notifications API 推送扫描结果到 Telegram。"""
    chat_id = os.environ.get("OWNER_CHAT_ID")
    if not chat_id:
        logger.warning("OWNER_CHAT_ID 未设置，跳过 Telegram 推送")
        return

    scan_date = summary["scan_date"]
    lines = [
        f"📊 A股 EOD 扫描完成 ({scan_date})",
        "",
        f"扫描: {summary['total_scanned']} | "
        f"趋势通过: {summary['trend_pass']} | "
        f"回调区间: {summary['pullback_zone']} | "
        f"触发: {summary['triggered']}",
    ]

    triggered = [c for c in summary.get("candidates", []) if c["stage"] == "TRIGGERED"]
    if triggered:
        lines.append("")
        lines.append("🎯 触发信号:")
        for i, c in enumerate(triggered, 1):
            lines.append(
                f"{i}. {c['name']}({c['symbol']}) | 回调至MA20\n"
                f"   入场: ¥{c.get('entry_price', 0):,.2f} | "
                f"止损: ¥{c.get('stop_price', 0):,.2f}\n"
                f"   风险: ¥{c.get('risk_amount_cny', 0):,.0f} | "
                f"{c.get('position_size', 0)}股 | "
                f"Flow: {c.get('flow_result', '-')}"
            )
    else:
        lines.append("")
        lines.append("暂无触发信号")

    body = "\n".join(lines)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_notifications (chat_id, title, body, source, status)
            VALUES (%s, %s, %s, %s, 'pending')
        """, (int(chat_id), f"A股扫描 {scan_date}", body, "strategy"))
        conn.commit()

    logger.info("Telegram 通知已创建")


def run_eod_scan(conn) -> Dict[str, Any]:
    """
    EOD 扫描主入口。

    流程:
    1. 加载策略配置
    2. 收集 Universe
    3. 过滤 ST/黑名单
    4. 拉取 OHLCV 数据
    5. 过滤流动性
    6. 逐标的: 技术扫描 → 量价确认 → 仓位计算
    7. 保存候选和订单草稿
    8. 推送 Telegram 通知

    Returns:
        扫描摘要 dict
    """
    today = date.today().isoformat()

    # 1. 加载配置
    config = load_strategy_config(conn)
    if not config:
        return {"error": "策略配置未找到", "scan_date": today}

    satellite = get_satellite_cn_config(config)
    if not satellite:
        return {"error": "satellite_cn 配置缺失或已禁用", "scan_date": today}

    b_nav = get_b_nav(config)
    risk_state = get_current_risk_state(conn, config)
    risk_budget = get_risk_budget(config, risk_state["risk_mode"])

    # 1b. 刷新市场情绪
    try:
        sent_snapshot = refresh_sentiment(conn)
        global_sent_state = sent_snapshot.get("sent_state", "NEUTRAL")
        global_sent_score = get_sent_score(global_sent_state, satellite)
    except Exception as e:
        logger.warning("情绪刷新失败，使用默认 NEUTRAL: %s", e)
        global_sent_state = "NEUTRAL"
        global_sent_score = 70.0

    # 1c. PANIC 模式 → 强制 defensive
    if global_sent_state == "PANIC":
        risk_state = {**risk_state, "risk_mode": "defensive"}
        risk_budget = get_risk_budget(config, "defensive")

    # 2. 收集 Universe (带来源标注)
    universe_map = get_universe_symbols(conn)
    if not universe_map:
        return {"scan_date": today, "total_scanned": 0, "error": "无候选 symbol"}

    symbols = list(universe_map.keys())

    # 3. 过滤 ST
    symbols, st_excluded = filter_st_delist(symbols)

    # 4. 过滤黑名单
    symbols, bl_excluded = filter_blacklist(conn, symbols)

    # 5. 拉取 OHLCV 数据
    logger.info("开始拉取 %d 个 symbol 的 OHLCV 数据...", len(symbols))
    fetch_and_store_batch(conn, symbols, days=120)

    # 5b. v1.2: 拉取指数 OHLCV（HS300，用于 RS 计算）
    index_ohlcv = get_index_ohlcv_from_db(conn, "sh000300", 120)
    if len(index_ohlcv) < 60:
        logger.info("指数数据不足，从 AkShare 获取 HS300...")
        try:
            idx_rows = fetch_index_ohlcv_akshare("sh000300", 120)
            if idx_rows:
                store_ohlcv(conn, idx_rows)
                index_ohlcv = get_index_ohlcv_from_db(conn, "sh000300", 120)
        except Exception as e:
            logger.warning("指数数据获取失败，RS 将使用默认值: %s", e)

    # 6. 过滤流动性
    min_turnover = satellite.get("universe", {}).get("liquidity_floor", {}).get("avg_turnover_20d_cny", 200_000_000)
    symbols, liq_excluded = filter_liquidity(conn, symbols, min_turnover)

    # 来源 → reason code 映射
    _SOURCE_RC = {
        "holdings":  "RC_U001_FROM_HOLDINGS",
        "watchlist": "RC_U001_FROM_WATCHLIST",
        "both":      "RC_U001_FROM_BOTH",
    }

    # 统计
    total_scanned = len(symbols) + len(st_excluded) + len(bl_excluded) + len(liq_excluded)
    universe_pass = len(symbols)
    trend_pass_count = 0
    pullback_zone_count = 0
    triggered_count = 0
    orders_created = 0
    all_candidates = []

    # 全局情绪字段 (注入每个候选)
    _global_sent = {
        "sent_state": global_sent_state,
        "sent_score": global_sent_score,
    }

    # 7. 逐标的技术扫描
    for symbol in symbols:
        name = _get_symbol_name(conn, symbol)
        source = universe_map.get(symbol, "watchlist")
        ohlcv = get_ohlcv_from_db(conn, symbol, days=120)
        indicators = compute_indicators(ohlcv, index_ohlcv)

        if not indicators:
            logger.warning("%s: 指标计算失败，跳过", symbol)
            continue

        reason_codes = [_SOURCE_RC.get(source, "RC_U001_FROM_WATCHLIST")]

        # 公共技术指标字段
        _tech = {k: indicators.get(k) for k in [
            "close", "ma20", "ma60", "atr14", "high_20d", "low_20d", "max_return_40d",
        ]}

        # 停牌检查
        is_suspended, susp_reason = check_suspension(symbol)
        if is_suspended:
            reason_codes = add_reason(reason_codes, "RC_U102_EXCLUDE_SUSP")
            candidate = {
                "symbol": symbol, "name": name, "market": "CN_A",
                "scan_date": today, "stage": "UNIVERSE_PASS",
                "reason_codes": reason_codes,
                **_tech, **_global_sent,
            }
            _save_candidate(conn, candidate)
            all_candidates.append(candidate)
            continue

        # 冷却期检查
        cooldown_blocked, cooldown_reasons = check_cooldown(conn, symbol)
        if cooldown_blocked:
            reason_codes = add_reasons(reason_codes, cooldown_reasons)
            candidate = {
                "symbol": symbol, "name": name, "market": "CN_A",
                "scan_date": today, "stage": "UNIVERSE_PASS",
                "reason_codes": reason_codes,
                **_tech, **_global_sent,
            }
            _save_candidate(conn, candidate)
            all_candidates.append(candidate)
            continue

        # 趋势门
        trend_ok, trend_reasons = check_trend_gate(indicators, satellite)
        reason_codes = add_reasons(reason_codes, trend_reasons)

        if not trend_ok:
            candidate = {
                "symbol": symbol, "name": name, "market": "CN_A",
                "scan_date": today, "stage": "UNIVERSE_PASS",
                "reason_codes": reason_codes,
                **_tech, **_global_sent,
            }
            _save_candidate(conn, candidate)
            all_candidates.append(candidate)
            continue

        trend_pass_count += 1

        # 回调区间 (v1.2: 传入 ohlcv 用于 5日 touch 检查)
        zone_ok, zone_reasons = check_pullback_zone(ohlcv, indicators, satellite)
        reason_codes = add_reasons(reason_codes, zone_reasons)

        if not zone_ok:
            cached_qs = _get_cached_quality_score(conn, symbol)
            candidate = {
                "symbol": symbol, "name": name, "market": "CN_A",
                "scan_date": today, "stage": "TREND_PASS",
                "reason_codes": reason_codes,
                "quality_score": cached_qs,
                **_tech, **_global_sent,
            }
            _save_candidate(conn, candidate)
            all_candidates.append(candidate)
            continue

        pullback_zone_count += 1

        # 触发日
        trigger_ok, trigger_reasons = check_trigger_day(indicators)
        reason_codes = add_reasons(reason_codes, trigger_reasons)

        if not trigger_ok:
            # PULLBACK_ZONE: 优先读缓存，无缓存则主动获取基本面
            cached_qs = _get_cached_quality_score(conn, symbol)
            if cached_qs is None:
                try:
                    fund = refresh_fundamental(conn, symbol)
                    cached_qs = fund.get("quality_score")
                except Exception as e:
                    logger.warning("%s: PULLBACK_ZONE 基本面获取失败: %s", symbol, e)
            candidate = {
                "symbol": symbol, "name": name, "market": "CN_A",
                "scan_date": today, "stage": "PULLBACK_ZONE",
                "reason_codes": reason_codes,
                "quality_score": cached_qs,
                **_tech, **_global_sent,
            }
            _save_candidate(conn, candidate)
            all_candidates.append(candidate)
            continue

        triggered_count += 1

        # 基本面门控
        fund_snapshot = refresh_fundamental(conn, symbol)
        quality_score = fund_snapshot.get("quality_score", 50.0)
        reason_codes = add_reasons(reason_codes, fund_snapshot.get("reason_codes", []))

        # Hard Veto 检查
        veto_hit, veto_reasons = check_hard_veto(conn, indicators, ohlcv, satellite)
        reason_codes = add_reasons(reason_codes, veto_reasons)

        # 事件否决检查
        event_veto, event_reasons = check_event_veto(conn, symbol, days_back=1)
        reason_codes = add_reasons(reason_codes, event_reasons)
        if event_veto:
            veto_hit = True
            reason_codes = add_reason(reason_codes, "RC_V003_MAJOR_NEG_EVENT_TPLUS1")

        # 量价确认
        flow = compute_flow_confirm(ohlcv, indicators, satellite)
        reason_codes = add_reasons(reason_codes, flow["reason_codes"])

        # 计算 entry/stop (v1.2: 传入 ohlcv + 12%拒绝处理)
        entry_stop = calc_entry_stop(ohlcv, indicators, satellite)
        reason_codes = add_reasons(reason_codes, entry_stop["reason_codes"])

        # v1.2: 止损过宽拒绝 → 保存 candidate 但标记 ineligible
        if entry_stop.get("rejected"):
            candidate = {
                "symbol": symbol, "name": name, "market": "CN_A",
                "scan_date": today, "stage": "TRIGGERED",
                "eligible": False, "hard_veto_hit": False,
                "reason_codes": reason_codes,
                "entry_price": entry_stop["entry_price"],
                "stop_price": entry_stop["stop_price"],
                **_tech, **_global_sent,
                "quality_score": quality_score,
                "flow_result": flow["flow_result"],
                "flow_score": flow.get("flow_score", 50),
            }
            _save_candidate(conn, candidate)
            all_candidates.append(candidate)
            continue

        # 仓位计算
        sizing = calculate_position_size(
            entry_stop["entry_price"],
            entry_stop["stop_price"],
            b_nav,
            risk_budget["per_trade_risk_pct"],
            config,
        )
        reason_codes = add_reasons(reason_codes, sizing["reason_codes"])

        # 并发风险检查
        conc_ok, conc_reasons = check_concurrent_risk(
            conn, config, sizing["risk_amount_cny"], risk_state["risk_mode"],
        )
        reason_codes = add_reasons(reason_codes, conc_reasons)

        # 主题敞口检查
        theme_ok, theme_reasons = check_theme_exposure(
            conn, config, symbol,
            sizing["position_size"] * entry_stop["entry_price"],
            risk_state["risk_mode"],
        )
        reason_codes = add_reasons(reason_codes, theme_reasons)

        # 情绪 reason code
        if global_sent_state == "HOT":
            reason_codes = add_reason(reason_codes, "RC_S101_SENT_HOT")
        elif global_sent_state == "PANIC":
            reason_codes = add_reason(reason_codes, "RC_S102_SENT_PANIC")
        else:
            reason_codes = add_reason(reason_codes, "RC_S001_SENT_NEUTRAL")

        # 个股热度检查 (拥挤交易预警)
        heat_rank = check_symbol_heat(symbol)
        if heat_rank is not None and heat_rank <= 20:
            reason_codes = add_reason(reason_codes, "RC_S103_CROWDED_TRADE")

        # HOT 模式 → 仓位减半 (保持 100 股整手)
        if global_sent_state == "HOT":
            hot_mult = satellite.get("sentiment", {}).get("rules", {}).get(
                "HOT", {},
            ).get("position_cap_multiplier", 0.5)
            reduced = int(sizing["position_size"] * hot_mult / 100) * 100
            sizing = {**sizing, "position_size": max(reduced, 100)}

        # 综合评分（三权重: flow × 0.5 + fundamental × 0.2 + sentiment × 0.3）
        flow_score = flow.get("flow_score", 50)
        weights = satellite.get("confirm_score", {}).get("weights", {})
        w_flow = weights.get("flow", 0.5)
        w_fund = weights.get("fundamental", 0.2)
        w_sent = weights.get("sent", 0.3)
        confirm_score = round(
            flow_score * w_flow + quality_score * w_fund + global_sent_score * w_sent,
            1,
        )

        # PANIC 模式 → 量价必须 PASS 才能 eligible
        panic_flow_ok = True
        if global_sent_state == "PANIC" and flow["flow_result"] != "PASS":
            panic_flow_ok = False

        # 判断 eligible
        eligible = (
            risk_state["can_open_new"]
            and not veto_hit
            and flow["flow_result"] != "FAIL"
            and sizing["position_size"] > 0
            and conc_ok
            and theme_ok
            and panic_flow_ok
        )

        reason_codes = add_reason(reason_codes, "RC_E001_ORDER_DRAFT_CREATED")
        reason_codes = add_reason(reason_codes, "RC_E002_MANUAL_CONFIRM_REQUIRED")

        candidate = {
            "symbol": symbol, "name": name, "market": "CN_A",
            "scan_date": today, "stage": "TRIGGERED",
            "close": indicators["close"],
            "ma20": indicators["ma20"], "ma60": indicators["ma60"],
            "atr14": indicators["atr14"],
            "high_20d": indicators["high_20d"], "low_20d": indicators["low_20d"],
            "max_return_40d": indicators["max_return_40d"],
            "flow_result": flow["flow_result"],
            "flow_score": flow_score,
            "confirm_score": confirm_score,
            "quality_score": quality_score,
            "sent_state": global_sent_state,
            "sent_score": global_sent_score,
            "eligible": eligible,
            "hard_veto_hit": veto_hit,
            "entry_price": entry_stop["entry_price"],
            "stop_price": entry_stop["stop_price"],
            "valid_until": entry_stop["valid_until"],
            "position_size": sizing["position_size"],
            "risk_amount_cny": sizing["risk_amount_cny"],
            "position_value_pct": sizing["position_value_pct"],
            "max_loss_cny": sizing["max_loss_cny"],
            "reason_codes": reason_codes,
        }

        candidate_id = _save_candidate(conn, candidate)
        all_candidates.append(candidate)

        # 保存订单草稿
        if eligible:
            _save_order_draft(conn, candidate, candidate_id, risk_state)
            orders_created += 1

    # 8. 汇总
    summary = {
        "scan_date": today,
        "total_scanned": total_scanned,
        "universe_pass": universe_pass,
        "trend_pass": trend_pass_count,
        "pullback_zone": pullback_zone_count,
        "triggered": triggered_count,
        "orders_created": orders_created,
        "candidates": all_candidates,
    }

    # 9. 推送通知
    try:
        _send_notification(conn, summary)
    except Exception as e:
        logger.error("推送通知失败: %s", e)

    logger.info(
        "EOD 扫描完成: 扫描=%d, 趋势=%d, 回调=%d, 触发=%d, 订单=%d",
        total_scanned, trend_pass_count, pullback_zone_count, triggered_count, orders_created,
    )

    return summary
