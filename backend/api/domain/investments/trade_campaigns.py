"""Read-only position campaign reconstruction for trade review.

A campaign is one lifecycle of an account/asset position: opening buys,
subsequent adds/reductions, and eventual close. The service computes campaigns
from immutable trade facts and never writes back to the database.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as Date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
from typing import Any
from zoneinfo import ZoneInfo
from domain.investments.trade_cost_evidence import known_trade_charges


MONEY = Decimal("0.01")
QTY = Decimal("0.000001")
EPSILON_QTY = Decimal("0.0000005")
TRADE_TYPES = ("buy", "sell", "fund_subscription", "fund_redemption")


def dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [norm(item) for item in value]
    if isinstance(value, dict):
        return {key: norm(item) for key, item in value.items()}
    return value


def today_local() -> Date:
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def q_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def q_qty(value: Decimal) -> Decimal:
    return value.quantize(QTY, rounding=ROUND_HALF_UP)


def _date(value: Any) -> Date | None:
    if isinstance(value, Date):
        return value
    if hasattr(value, "date"):
        return value.date()
    if value:
        return Date.fromisoformat(str(value)[:10])
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _asset_key(row: dict[str, Any]) -> str:
    return (_text(row.get("symbol")) or _text(row.get("name"))).lower()


def _side(row: dict[str, Any]) -> str:
    trade_type = str(row.get("trade_type") or "").lower()
    side = str(row.get("side") or "").lower()
    if trade_type in {"buy", "fund_subscription"} or side in {"buy", "bought"}:
        return "buy"
    if trade_type in {"sell", "fund_redemption"} or side in {"sell", "sold"}:
        return "sell"
    return side or trade_type or "unknown"


def _group_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _text(row.get("account_code")).lower(),
        _asset_key(row),
        _text(row.get("currency")).upper() or "CNY",
    )


def _gross_amount(row: dict[str, Any]) -> Decimal:
    gross = abs(dec(row.get("gross_amount")))
    if gross > 0:
        return gross
    quantity = abs(dec(row.get("quantity")))
    price = abs(dec(row.get("price")))
    return quantity * price


def _fees(row: dict[str, Any]) -> Decimal | None:
    return known_trade_charges(row)


def _quantity(row: dict[str, Any]) -> Decimal:
    return abs(dec(row.get("quantity")))


def _trade_time_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(row.get("trade_date") or ""),
        str(row.get("trade_time") or row.get("trade_date") or ""),
        int(dec(row.get("trade_id"))),
    )


def _event(row: dict[str, Any], *, side: str, amount: Decimal | None, fees: Decimal | None) -> dict[str, Any]:
    return {
        "trade_id": row.get("trade_id"),
        "trade_date": row.get("trade_date"),
        "side": side,
        "trade_type": row.get("trade_type"),
        "quantity": q_qty(_quantity(row)),
        "price": dec(row.get("price")) if row.get("price") not in (None, "") else None,
        "amount": q_money(amount) if amount is not None else None,
        "fees": q_money(fees) if fees is not None else None,
        "buy_reason": row.get("buy_reason"),
        "sell_reason": row.get("sell_reason"),
        "review_tags": list(row.get("review_tags") or []),
        "initial_position_identity": row.get("initial_position_identity"),
        "current_position_identity": row.get("current_position_identity"),
        "review_status": row.get("review_status"),
    }


def _new_campaign(row: dict[str, Any], ordinal: int) -> dict[str, Any]:
    trade_day = _date(row.get("trade_date"))
    account_code, asset_key, currency = _group_key(row)
    return {
        "campaign_id": f"{account_code}:{asset_key}:{trade_day or 'unknown'}:{ordinal}",
        "account_code": row.get("account_code"),
        "account_name": row.get("account_name") or row.get("account_code"),
        "asset_key": asset_key,
        "symbol": row.get("symbol"),
        "name": row.get("name") or row.get("symbol"),
        "currency": currency,
        "start_date": trade_day,
        "end_date": None,
        "latest_trade_date": trade_day,
        "status": "open",
        "buy_count": 0,
        "sell_count": 0,
        "add_count": 0,
        "reduce_count": 0,
        "trade_count": 0,
        "total_buy_quantity": Decimal("0"),
        "total_sell_quantity": Decimal("0"),
        "open_quantity": Decimal("0"),
        "total_buy_cost": Decimal("0"),
        "total_sell_proceeds": Decimal("0"),
        "open_cost_basis": Decimal("0"),
        "realized_pnl_estimated": Decimal("0"),
        "realized_pnl_reported": Decimal("0"),
        "fees": Decimal("0"),
        "unmatched_sell_quantity": Decimal("0"),
        "amount_only_trade_count": 0,
        "events": [],
        "lot_matches": [],
        "warnings": [],
        "_fees_unknown": False,
        "_lots": [],
    }


def _update_status(campaign: dict[str, Any]) -> None:
    has_quantity_buys = campaign["total_buy_quantity"] > EPSILON_QTY
    has_open = campaign["open_quantity"] > EPSILON_QTY
    if campaign["buy_count"] == 0 and campaign["sell_count"] > 0:
        campaign["status"] = "orphan_sell"
    elif campaign["amount_only_trade_count"] > 0 and not has_quantity_buys:
        campaign["status"] = "amount_only"
    elif has_open:
        campaign["status"] = "open"
    else:
        campaign["status"] = "closed"


def _add_buy(campaign: dict[str, Any], row: dict[str, Any]) -> None:
    quantity = _quantity(row)
    gross = _gross_amount(row)
    known_fees = _fees(row)
    if known_fees is None:
        campaign['_fees_unknown'] = True
    fees = known_fees if known_fees is not None else Decimal('0')
    cost = gross + fees
    campaign["buy_count"] += 1
    campaign["trade_count"] += 1
    campaign["fees"] += fees
    campaign["total_buy_quantity"] += quantity
    campaign["total_buy_cost"] += cost
    campaign["events"].append(_event(row, side="buy", amount=cost if known_fees is not None else None, fees=known_fees))
    if quantity > EPSILON_QTY:
        campaign["open_quantity"] += quantity
        campaign["open_cost_basis"] += cost
        campaign["_lots"].append(
            {
                "trade_id": row.get("trade_id"),
                "trade_date": row.get("trade_date"),
                "quantity": quantity,
                "remaining_quantity": quantity,
                "cost": cost,
                "remaining_cost": cost,
            }
        )
    else:
        campaign["amount_only_trade_count"] += 1
        campaign["warnings"].append(f"buy trade {row.get('trade_id')} has no quantity; lot matching skipped")
    campaign["add_count"] = max(campaign["buy_count"] - 1, 0)


def _add_sell(campaign: dict[str, Any], row: dict[str, Any]) -> None:
    quantity = _quantity(row)
    gross = _gross_amount(row)
    known_fees = _fees(row)
    if known_fees is None:
        campaign['_fees_unknown'] = True
    fees = known_fees if known_fees is not None else Decimal('0')
    proceeds = gross - fees
    campaign["sell_count"] += 1
    campaign["reduce_count"] += 1
    campaign["trade_count"] += 1
    campaign["fees"] += fees
    campaign["total_sell_quantity"] += quantity
    campaign["total_sell_proceeds"] += proceeds
    campaign["realized_pnl_reported"] += dec(row.get("realized_pnl"))
    campaign["events"].append(_event(row, side="sell", amount=proceeds if known_fees is not None else None, fees=known_fees))
    if quantity <= EPSILON_QTY:
        campaign["amount_only_trade_count"] += 1
        campaign["warnings"].append(f"sell trade {row.get('trade_id')} has no quantity; lot matching skipped")
        return
    remaining_sell_qty = quantity
    proceeds_per_unit = proceeds / quantity if quantity > 0 else Decimal("0")
    for lot in campaign["_lots"]:
        lot_qty = lot["remaining_quantity"]
        if remaining_sell_qty <= EPSILON_QTY:
            break
        if lot_qty <= EPSILON_QTY:
            continue
        matched_qty = min(lot_qty, remaining_sell_qty)
        cost_per_unit = lot["remaining_cost"] / lot_qty if lot_qty > 0 else Decimal("0")
        matched_cost = cost_per_unit * matched_qty
        matched_proceeds = proceeds_per_unit * matched_qty
        pnl = matched_proceeds - matched_cost
        lot["remaining_quantity"] -= matched_qty
        lot["remaining_cost"] -= matched_cost
        remaining_sell_qty -= matched_qty
        campaign["open_quantity"] -= matched_qty
        campaign["open_cost_basis"] -= matched_cost
        campaign["realized_pnl_estimated"] += pnl
        campaign["lot_matches"].append(
            {
                "sell_trade_id": row.get("trade_id"),
                "buy_trade_id": lot.get("trade_id"),
                "quantity": q_qty(matched_qty),
                "cost_basis": q_money(matched_cost),
                "proceeds": q_money(matched_proceeds),
                "realized_pnl": q_money(pnl),
            }
        )
    if remaining_sell_qty > EPSILON_QTY:
        campaign["unmatched_sell_quantity"] += remaining_sell_qty
        campaign["warnings"].append(
            f"sell trade {row.get('trade_id')} exceeds open FIFO quantity by {q_qty(remaining_sell_qty)}"
        )
    if campaign["open_quantity"] < EPSILON_QTY:
        campaign["open_quantity"] = Decimal("0")
    if campaign["open_cost_basis"] < MONEY:
        campaign["open_cost_basis"] = Decimal("0")


def _finalize_campaign(campaign: dict[str, Any]) -> dict[str, Any]:
    _update_status(campaign)
    if campaign["status"] == "closed":
        campaign["end_date"] = campaign["latest_trade_date"]
    total_buy_qty = campaign["total_buy_quantity"]
    total_sell_qty = campaign["total_sell_quantity"]
    campaign["avg_entry_price"] = (
        q_money(campaign["total_buy_cost"] / total_buy_qty)
        if total_buy_qty > EPSILON_QTY
        else None
    )
    campaign["avg_exit_price"] = (
        q_money(campaign["total_sell_proceeds"] / total_sell_qty)
        if total_sell_qty > EPSILON_QTY
        else None
    )
    for key in (
        "total_buy_quantity",
        "total_sell_quantity",
        "open_quantity",
        "unmatched_sell_quantity",
    ):
        campaign[key] = q_qty(campaign[key])
    for key in (
        "total_buy_cost",
        "total_sell_proceeds",
        "open_cost_basis",
        "realized_pnl_estimated",
        "realized_pnl_reported",
        "fees",
    ):
        campaign[key] = q_money(campaign[key])
    if campaign.pop('_fees_unknown'):
        campaign['fee_evidence'] = 'unknown'
        campaign['warnings'].append('transaction fees or tax are unknown; financial totals are unavailable')
        for key in ('total_buy_cost', 'total_sell_proceeds', 'open_cost_basis',
                    'realized_pnl_estimated', 'fees', 'avg_entry_price', 'avg_exit_price'):
            campaign[key] = None
        for match in campaign['lot_matches']:
            for key in ('cost_basis', 'proceeds', 'realized_pnl'):
                match[key] = None
    else:
        campaign['fee_evidence'] = 'complete'
    campaign.pop("_lots", None)
    campaign["events"] = [{k: norm(v) for k, v in event.items()} for event in campaign["events"]]
    campaign["lot_matches"] = [{k: norm(v) for k, v in match.items()} for match in campaign["lot_matches"]]
    return {k: norm(v) for k, v in campaign.items()}


def build_position_campaigns_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if _side(row) in {"buy", "sell"} and _asset_key(row):
            grouped[_group_key(row)].append(row)

    campaigns: list[dict[str, Any]] = []
    for _, group_rows in sorted(grouped.items(), key=lambda item: item[0]):
        current: dict[str, Any] | None = None
        ordinal = 0
        for row in sorted(group_rows, key=_trade_time_key):
            side = _side(row)
            if current is None:
                ordinal += 1
                current = _new_campaign(row, ordinal)
            trade_day = _date(row.get("trade_date"))
            current["latest_trade_date"] = trade_day or current["latest_trade_date"]
            if side == "buy":
                _add_buy(current, row)
            elif side == "sell":
                _add_sell(current, row)
            _update_status(current)
            if current["status"] in {"closed", "orphan_sell"}:
                campaigns.append(_finalize_campaign(current))
                current = None
        if current is not None:
            campaigns.append(_finalize_campaign(current))
    campaigns.sort(key=lambda item: (str(item.get("latest_trade_date") or ""), str(item.get("campaign_id") or "")), reverse=True)
    return campaigns


def _campaign_intersects(campaign: dict[str, Any], *, start: Date | None, end: Date | None) -> bool:
    start_date = _date(campaign.get("start_date"))
    latest_date = _date(campaign.get("latest_trade_date")) or start_date
    if start and latest_date and latest_date < start:
        return False
    if end and start_date and start_date > end:
        return False
    return True


def _summary(campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = defaultdict(int)
    currency_totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "realized_pnl_estimated": Decimal("0"),
            "realized_pnl_reported": Decimal("0"),
        }
    )
    unmatched = Decimal("0")
    for campaign in campaigns:
        status_counts[str(campaign.get("status") or "unknown")] += 1
        currency = str(campaign.get("currency") or "UNKNOWN")
        if campaign.get('realized_pnl_estimated') is None:
            currency_totals[currency]['realized_pnl_estimated'] = None
        elif currency_totals[currency]['realized_pnl_estimated'] is not None:
            currency_totals[currency]["realized_pnl_estimated"] += dec(campaign.get("realized_pnl_estimated"))
        currency_totals[currency]["realized_pnl_reported"] += dec(campaign.get("realized_pnl_reported"))
        unmatched += dec(campaign.get("unmatched_sell_quantity"))
    single_currency = len(currency_totals) == 1
    only_totals = next(iter(currency_totals.values())) if single_currency else None
    return {
        "total_campaigns": len(campaigns),
        "status_counts": dict(status_counts),
        "open_campaigns": status_counts.get("open", 0),
        "closed_campaigns": status_counts.get("closed", 0),
        "amount_only_campaigns": status_counts.get("amount_only", 0),
        "orphan_sell_campaigns": status_counts.get("orphan_sell", 0),
        "amount_scope": "original_currency",
        "realized_pnl_estimated": q_money(only_totals["realized_pnl_estimated"]) if only_totals and only_totals["realized_pnl_estimated"] is not None else None,
        "realized_pnl_reported": q_money(only_totals["realized_pnl_reported"]) if only_totals else None,
        "currency_totals": {
            currency: {
                "realized_pnl_estimated": q_money(totals["realized_pnl_estimated"]) if totals["realized_pnl_estimated"] is not None else None,
                "realized_pnl_reported": q_money(totals["realized_pnl_reported"]),
            }
            for currency, totals in sorted(currency_totals.items())
        },
        "unmatched_sell_quantity": q_qty(unmatched),
    }


def build_position_campaigns_payload(
    cur,
    *,
    start: Date | None = None,
    end: Date | None = None,
    account_code: str | None = None,
    asset_key: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    where = ["f.trade_type IN %s"]
    params: list[Any] = [TRADE_TYPES]
    if end:
        where.append("f.trade_date <= %s")
        params.append(end)
    if account_code:
        where.append("f.account_code = %s")
        params.append(account_code)
    if asset_key:
        where.append("LOWER(COALESCE(NULLIF(f.symbol, ''), NULLIF(f.name, ''), '')) = %s")
        params.append(asset_key.lower())
    cur.execute(
        f"""
        SELECT
            f.trade_id,
            f.trade_date,
            COALESCE(f.trade_time, f.trade_date::timestamp) AS trade_time,
            f.account_code,
            COALESCE(a.display_name, f.account_code) AS account_name,
            f.symbol,
            f.name,
            f.currency,
            f.trade_type,
            f.side,
            f.quantity,
            f.price,
            f.gross_amount,
            f.fee_amount,
            f.tax_amount,
            f.raw_json,
            f.net_cash,
            f.realized_pnl,
            f.realized_pnl_cny,
            f.buy_reason,
            f.sell_reason,
            COALESCE(f.review_tags, ARRAY[]::text[]) AS review_tags,
            f.initial_position_identity,
            f.current_position_identity,
            CASE
                WHEN f.review_status IN ('auto', 'needs_review', 'reviewed', 'ignored') THEN f.review_status
                ELSE 'auto'
            END AS review_status
        FROM pams_investment_trade_fact f
        LEFT JOIN pams_accounts a ON a.account_code = f.account_code
        WHERE {" AND ".join(where)}
        ORDER BY f.account_code, COALESCE(NULLIF(f.symbol, ''), f.name), f.trade_date, f.trade_time, f.trade_id
        """,
        params,
    )
    rows = [dict(row) for row in cur.fetchall()]
    campaigns = [
        campaign
        for campaign in build_position_campaigns_from_rows(rows)
        if _campaign_intersects(campaign, start=start, end=end)
    ]
    if status and status != "all":
        campaigns = [campaign for campaign in campaigns if campaign.get("status") == status]
    summary = _summary(campaigns)
    visible = campaigns[:limit]
    return {
        "as_of": today_local().isoformat(),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "account_code": account_code,
        "asset_key": asset_key,
        "status": status or "all",
        "limit": limit,
        "summary": {k: norm(v) for k, v in summary.items()},
        "campaigns": visible,
        "data_quality": {
            "note": "Read-only FIFO approximation. Amount-only fund trades without quantity are grouped but not lot-matched.",
        },
    }
