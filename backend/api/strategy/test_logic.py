"""
上线脚本：将用户指定的 4 笔入场价写入系统。
更新候选为 TRIGGERED → 创建 pending 订单草稿 → 推送 Telegram。
"""
import json
import logging
import math
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

from strategy.config import (
    add_reason, add_reasons, load_strategy_config,
    get_b_nav, get_risk_budget, get_satellite_cn_config,
)
from strategy.indicators import compute_indicators
from strategy.risk_sizing import get_current_risk_state, calculate_position_size
from strategy.data_feed import get_ohlcv_from_db
from strategy.flow_confirm import compute_flow_confirm

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MIN_LOT = 100  # A股最小交易单位


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


# 用户指定的入场价
MANUAL_ENTRIES = [
    {"symbol": "sz000735", "name": "罗牛山",   "entry_price": 10.418},
    {"symbol": "sh601899", "name": "紫金矿业", "entry_price": 38.20},
    {"symbol": "sz002594", "name": "比亚迪",   "entry_price": 93.999},
    {"symbol": "sz000630", "name": "铜陵有色", "entry_price": 8.88},
]


def upsert_candidate(conn, c):
    """UPSERT 候选到 pams_strategy_candidates，返回 id。"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_strategy_candidates (
                symbol, name, market, scan_date, stage,
                close, ma20, ma60, atr14, high_20d, low_20d, max_return_40d,
                flow_result, flow_score, confirm_score,
                eligible, hard_veto_hit, reason_codes,
                entry_price, stop_price, valid_until, position_size, risk_amount_cny
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
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
                eligible = EXCLUDED.eligible, hard_veto_hit = EXCLUDED.hard_veto_hit,
                reason_codes = EXCLUDED.reason_codes,
                entry_price = EXCLUDED.entry_price, stop_price = EXCLUDED.stop_price,
                valid_until = EXCLUDED.valid_until,
                position_size = EXCLUDED.position_size,
                risk_amount_cny = EXCLUDED.risk_amount_cny
            RETURNING id
        """, (
            c["symbol"], c["name"], c["market"],
            c["scan_date"], c["stage"],
            c["close"], c["ma20"], c["ma60"],
            c["atr14"], c["high_20d"], c["low_20d"], c["max_return_40d"],
            c.get("flow_result"), c.get("flow_score"),
            c.get("confirm_score"),
            c["eligible"], c.get("hard_veto_hit", False),
            json.dumps(c["reason_codes"]),
            c["entry_price"], c["stop_price"],
            c["valid_until"], c["position_size"],
            c.get("risk_amount_cny"),
        ))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0


def insert_order_draft(conn, c, candidate_id, risk_state):
    """INSERT 订单草稿到 pams_order_drafts，返回 id。"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_order_drafts (
                candidate_id, symbol, market, b_nav, risk_mode,
                entry_price, stop_price, valid_until, position_size,
                position_value_pct, max_loss_cny, risk_amount_cny,
                concurrent_risk_after,
                flow_confirm, confirm_score,
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
            candidate_id, c["symbol"], c["market"],
            risk_state["b_nav"], risk_state["risk_mode"],
            c["entry_price"], c["stop_price"],
            c["valid_until"], c["position_size"],
            c.get("position_value_pct"), c.get("max_loss_cny"),
            c.get("risk_amount_cny"),
            risk_state["concurrent_risk_pct"],
            c.get("flow_result"), c.get("confirm_score"),
            c["eligible"], c.get("hard_veto_hit", False),
            json.dumps(c["reason_codes"]),
        ))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0


def send_notification(conn, results, b_nav):
    """推送 Telegram 通知。"""
    chat_id = os.environ.get("OWNER_CHAT_ID")
    if not chat_id:
        print("  ⚠ OWNER_CHAT_ID 未设置，跳过 Telegram 推送")
        return

    today = date.today().isoformat()
    lines = [
        f"🚀 手动入场单上线 ({today})",
        "",
        f"共 {len(results)} 笔订单草稿已创建:",
    ]
    for i, r in enumerate(results, 1):
        pos_value = r["position_size"] * r["entry_price"]
        lines.append(
            f"\n{i}. {r['name']}({r['symbol']})\n"
            f"   入场: ¥{r['entry_price']:,.3f} | "
            f"止损: ¥{r['stop_price']:,.2f}\n"
            f"   {r['position_size']}股 | "
            f"¥{pos_value:,.0f} ({r['position_value_pct']*100:.1f}% B_NAV)\n"
            f"   Flow: {r['flow_result']} | "
            f"评分: {r['confirm_score']}"
        )

    total_value = sum(r["position_size"] * r["entry_price"] for r in results)
    total_risk = sum(r["risk_amount_cny"] for r in results)
    lines.append(f"\n总市值: ¥{total_value:,.0f} ({total_value/b_nav*100:.1f}% B_NAV)")
    lines.append(f"总风险: ¥{total_risk:,.0f} ({total_risk/b_nav*100:.2f}% B_NAV)")
    lines.append("\n请在前端确认或取消。")

    body = "\n".join(lines)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_notifications (chat_id, title, body, source, status)
            VALUES (%s, %s, %s, %s, 'pending')
        """, (int(chat_id), f"手动入场单 {today}", body, "strategy_manual"))
        conn.commit()

    print("  ✓ Telegram 通知已创建")


def main():
    conn = get_conn()
    today = date.today().isoformat()

    # 加载配置
    config = load_strategy_config(conn)
    satellite = get_satellite_cn_config(config)
    b_nav = get_b_nav(config)
    risk_state = get_current_risk_state(conn, config)
    risk_budget = get_risk_budget(config, risk_state["risk_mode"])
    risk_pct = risk_budget["per_trade_risk_pct"]

    stop_config = satellite.get("technical_engine", {}).get("stop", {})
    rule_a_mult = stop_config.get("rule_a", {}).get("mult", 2.0)
    rule_b_mult = stop_config.get("rule_b", {}).get("mult", 0.5)
    valid_days = satellite.get("technical_engine", {}).get(
        "pullback_setup", {}
    ).get("valid_until_days", 2)
    valid_until = (date.today() + timedelta(days=valid_days)).isoformat()

    print("=" * 60)
    print("  手动入场单上线")
    print("=" * 60)
    print(f"  B_NAV: ¥{b_nav:,.2f} | 风险: {risk_pct*100:.1f}% "
          f"(¥{b_nav * risk_pct:,.2f}/笔)")
    print(f"  模式: {risk_state['risk_mode']} | 熔断: {risk_state['circuit_breaker']}")
    print(f"  有效期: {valid_until}")
    print("=" * 60)

    all_results = []

    for entry in MANUAL_ENTRIES:
        symbol = entry["symbol"]
        name = entry["name"]
        entry_price = entry["entry_price"]

        print(f"\n--- {name} ({symbol}) entry=¥{entry_price:.3f} ---")

        # 1. 指标
        ohlcv = get_ohlcv_from_db(conn, symbol, days=120)
        indicators = compute_indicators(ohlcv)
        if not indicators:
            print(f"  ✗ 指标计算失败，跳过")
            continue

        atr = indicators["atr14"]
        low_20d = indicators["low_20d"]
        reason_codes = ["RC_U001_ELIGIBLE", "RC_T001_TREND_GATE_PASS",
                        "RC_T201_PULLBACK_ZONE_HIT", "RC_T202_PULLBACK_TRIGGER_OK"]

        # 2. 止损
        stop_a = round(entry_price - rule_a_mult * atr, 2)
        stop_b = round(low_20d - rule_b_mult * atr, 2)
        stop_price = min(stop_a, stop_b)
        risk_per_share = entry_price - stop_price
        print(f"  止损: ¥{stop_price:.2f} (A={stop_a:.2f} B={stop_b:.2f})")

        # 3. 量价确认（仅记录，不阻断）
        flow = compute_flow_confirm(ohlcv, indicators, satellite)
        reason_codes = add_reasons(reason_codes, flow["reason_codes"])
        print(f"  Flow: {flow['flow_result']} ({flow['flow_score']})")

        # 4. 仓位计算
        sizing = calculate_position_size(
            entry_price, stop_price, b_nav, risk_pct, config,
        )
        position_size = sizing["position_size"]

        # 最小一手保底（用户手动覆盖时允许超出单笔风险预算）
        if position_size == 0 and risk_per_share > 0:
            position_size = MIN_LOT
            reason_codes = add_reason(reason_codes, "RC_R103_POSITION_VALUE_CAP")
            print(f"  ⚠ 风险预算不足一手，强制 {MIN_LOT} 股（用户覆盖）")

        pos_value = position_size * entry_price
        pos_value_pct = round(pos_value / b_nav, 4) if b_nav > 0 else 0
        max_loss = round(position_size * risk_per_share, 2)
        risk_amount = max_loss

        reason_codes = add_reasons(reason_codes, sizing["reason_codes"])
        reason_codes = add_reason(reason_codes, "RC_T301_ENTRY_LIMIT_SET")
        reason_codes = add_reason(reason_codes, "RC_E001_ORDER_DRAFT_CREATED")
        reason_codes = add_reason(reason_codes, "RC_E002_MANUAL_CONFIRM_REQUIRED")

        flow_score = flow.get("flow_score", 50)
        quality_score = 50.0  # test 简化: 无基本面数据时默认 50
        sent_score = 50.0     # test 简化: 无情绪数据时默认 50
        w_flow = satellite.get("confirm_score", {}).get("weights", {}).get("flow", 0.5)
        w_fund = satellite.get("confirm_score", {}).get("weights", {}).get("fundamental", 0.2)
        w_sent = satellite.get("confirm_score", {}).get("weights", {}).get("sent", 0.3)
        confirm_score = round(
            flow_score * w_flow + quality_score * w_fund + sent_score * w_sent, 1,
        )

        print(f"  仓位: {position_size}股 × ¥{entry_price:.3f} = ¥{pos_value:,.0f} "
              f"({pos_value_pct*100:.1f}%)")
        print(f"  风险: ¥{risk_amount:,.2f} | 评分: {confirm_score}")

        # 5. 写入候选
        candidate = {
            "symbol": symbol, "name": name, "market": "CN_A",
            "scan_date": today, "stage": "TRIGGERED",
            "close": indicators["close"],
            "ma20": indicators["ma20"], "ma60": indicators["ma60"],
            "atr14": atr, "high_20d": indicators["high_20d"],
            "low_20d": low_20d, "max_return_40d": indicators["max_return_40d"],
            "flow_result": flow["flow_result"],
            "flow_score": flow_score,
            "confirm_score": confirm_score,
            "eligible": True,
            "hard_veto_hit": False,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "valid_until": valid_until,
            "position_size": position_size,
            "risk_amount_cny": risk_amount,
            "position_value_pct": pos_value_pct,
            "max_loss_cny": max_loss,
            "reason_codes": reason_codes,
        }
        cid = upsert_candidate(conn, candidate)
        print(f"  ✓ 候选 id={cid} → TRIGGERED")

        # 6. 写入订单草稿
        oid = insert_order_draft(conn, candidate, cid, risk_state)
        print(f"  ✓ 订单草稿 id={oid} → pending")

        all_results.append(candidate)

    # 汇总
    print(f"\n{'='*60}")
    print(f"  汇总: {len(all_results)} 笔订单已创建")
    print(f"{'='*60}")
    for r in all_results:
        pv = r["position_size"] * r["entry_price"]
        print(f"  {r['name']:<6} ¥{r['entry_price']:>8.3f} → ¥{r['stop_price']:>7.2f} "
              f"| {r['position_size']:>4}股 ¥{pv:>8,.0f} "
              f"| Flow={r['flow_result']:<4} 评分={r['confirm_score']}")

    total_v = sum(r["position_size"] * r["entry_price"] for r in all_results)
    total_r = sum(r["risk_amount_cny"] for r in all_results)
    print(f"\n  总市值: ¥{total_v:,.0f} ({total_v/b_nav*100:.1f}% B_NAV)")
    print(f"  总风险: ¥{total_r:,.0f} ({total_r/b_nav*100:.2f}% B_NAV)")

    # 推送通知
    send_notification(conn, all_results, b_nav)

    conn.close()
    print("\n  Done.")


if __name__ == "__main__":
    main()
