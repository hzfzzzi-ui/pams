"""Read-only IBKR Flex performance parsing shared by endpoints and risk."""
from __future__ import annotations

import os
import hashlib
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2

from collectors.ibkr_flex import IBKRFlexError
from services import ibkr_report_state as report_state

ROOT = Path(__file__).resolve().parents[3]


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _norm(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value).replace(",", ""))
    except Exception:
        return default


def _ib_date(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _latest_flex_xml_path() -> Path | None:
    with get_conn() as conn:
        report, _ = report_state.select_report(conn, _raw_dir())
    return report.path if report else None


def _raw_dir() -> Path:
    data_dir = Path(os.environ.get("PAMS_DATA_DIR") or (ROOT / "data"))
    return data_dir / "raw" / "ibkr"


def published_report_status(conn=None) -> dict[str, Any]:
    if conn is None:
        with get_conn() as connection:
            return published_report_status(connection)
    report, latest = report_state.select_report(conn, _raw_dir())
    return report_state.report_quality(report, latest)


def read_published_performance() -> dict[str, Any] | None:
    with get_conn() as conn:
        report, latest = report_state.select_report(conn, _raw_dir())
    if report is None:
        return None
    result = _parse_flex_performance(report.path)
    if hashlib.sha256(report.path.read_bytes()).hexdigest() != report.sha256:
        raise IBKRFlexError("Published IBKR report changed during read")
    result["report_quality"] = report_state.report_quality(report, latest)
    return result


def _latest_usd_cny_rate() -> Decimal | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rate
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                  AND quote_currency = 'USD'
                ORDER BY as_of DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    return _dec(row[0]) if row else None


def _parse_flex_performance(path: Path, *, include_display_fx: bool = True) -> dict[str, Any]:
    root = ET.parse(path).getroot()

    cash_rows = [
        elem.attrib
        for elem in root.iter("CashReportCurrency")
        if elem.attrib.get("currency") == "BASE_SUMMARY"
    ]
    if not cash_rows:
        raise IBKRFlexError("IBKR Flex report has no BASE_SUMMARY cash report")

    cash_rows.sort(key=lambda row: row.get("toDate", ""))
    latest_cash = cash_rows[-1]
    report_date = latest_cash.get("toDate")

    positions = [
        elem.attrib
        for elem in root.iter("OpenPosition")
        if not report_date or elem.attrib.get("reportDate") == report_date
    ]
    if not positions:
        latest_position_date = max((elem.attrib.get("reportDate", "") for elem in root.iter("OpenPosition")), default="")
        report_date = latest_position_date or report_date
        positions = [
            elem.attrib
            for elem in root.iter("OpenPosition")
            if elem.attrib.get("reportDate") == report_date
        ]

    position_value_base = sum(
        _dec(row.get("positionValue")) * _dec(row.get("fxRateToBase"), Decimal("1"))
        for row in positions
    )
    gross_exposure_base = sum(
        abs(_dec(row.get("positionValue")) * _dec(row.get("fxRateToBase"), Decimal("1")))
        for row in positions
    )
    unrealized_pnl_base = sum(
        _dec(row.get("fifoPnlUnrealized")) * _dec(row.get("fxRateToBase"), Decimal("1"))
        for row in positions
    )

    ending_cash_base = _dec(latest_cash.get("endingCash"))
    net_liquidation_base = position_value_base + ending_cash_base
    net_deposit_base = _dec(latest_cash.get("depositWithdrawalsYTD"))
    total_pnl_base = net_liquidation_base - net_deposit_base

    realized_pnl_base = Decimal("0")
    for elem in root.iter("Trade"):
        row = elem.attrib
        if report_date and row.get("tradeDate", "") > report_date:
            continue
        realized_pnl_base += _dec(row.get("fifoPnlRealized")) * _dec(row.get("fxRateToBase"), Decimal("1"))

    other_pnl_base = total_pnl_base - unrealized_pnl_base - realized_pnl_base
    leverage_ratio = gross_exposure_base / net_liquidation_base if net_liquidation_base > 0 else Decimal("0")
    return_ratio = total_pnl_base / net_deposit_base if net_deposit_base > 0 else Decimal("0")
    negative_cash_ratio = abs(ending_cash_base) / net_liquidation_base if ending_cash_base < 0 and net_liquidation_base > 0 else Decimal("0")

    usd_cny = _latest_usd_cny_rate() if include_display_fx else None
    payload: dict[str, Any] = {
        "status": "ok",
        "as_of": _ib_date(report_date),
        "base_currency": "USD",
        "source_file": path.name,
        "net_deposit_base": net_deposit_base,
        "net_liquidation_base": net_liquidation_base,
        "total_pnl_base": total_pnl_base,
        "return_ratio": return_ratio,
        "gross_exposure_base": gross_exposure_base,
        "leverage_ratio": leverage_ratio,
        "negative_cash_base": abs(ending_cash_base) if ending_cash_base < 0 else Decimal("0"),
        "negative_cash_ratio": negative_cash_ratio,
        "cash_base": ending_cash_base,
        "position_value_base": position_value_base,
        "unrealized_pnl_base": unrealized_pnl_base,
        "realized_pnl_base": realized_pnl_base,
        "other_pnl_base": other_pnl_base,
        "position_count": len(positions),
    }
    if usd_cny is not None:
        payload["fx_rate_to_cny"] = usd_cny
        for key in (
            "net_deposit",
            "net_liquidation",
            "total_pnl",
            "gross_exposure",
            "negative_cash",
            "cash",
            "position_value",
            "unrealized_pnl",
            "realized_pnl",
            "other_pnl",
        ):
            payload[f"{key}_cny"] = payload[f"{key}_base"] * usd_cny
    return {k: _norm(v) for k, v in payload.items()}
