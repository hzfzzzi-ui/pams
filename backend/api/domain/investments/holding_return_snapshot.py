"""Precomputed holding-return snapshots for investment return charts."""

from __future__ import annotations

import hashlib
import json
from datetime import date as Date
from decimal import Decimal
from typing import Any

from domain.investments.investment_return_math import annualized_rate, dec, norm


SNAPSHOT_SECTION_TOTAL = "total"
SNAPSHOT_METHOD = "holding_return_snapshot"
SNAPSHOT_SECTIONS = {"total", "a_stock", "a_fund", "us_stock", "precious_metal"}

_TOTAL_REQUIRED = ("market_value", "cost", "position_pnl", "realized_income", "total_pnl",
                   "return_rate", "daily_return_rate", "time_weighted_return_rate")
_SECTION_REQUIRED = ("market_value", "net_flow", "cumulative_capital", "capital_return_rate",
                     "daily_return_rate", "time_weighted_return_rate", "display_return_rate")
_INCOMPLETE_STATES = {"unavailable", "unknown", "incomplete", "partial", "estimated", "pending"}


def _nullable_decimal(value: Any) -> Decimal | None:
    return None if value is None or value == "" else dec(value)


def _nullable_norm(value: Any) -> Any:
    number = _nullable_decimal(value)
    return norm(number) if number is not None else None


def _field(row: dict[str, Any], name: str, fallback: Any = None) -> Decimal | None:
    # Only an absent field may use an equivalent fallback; explicit NULL stays unknown.
    return _nullable_decimal(row[name] if name in row else fallback)


def _incomplete_snapshot_row(row: dict[str, Any], required: tuple[str, ...]) -> bool:
    if row.get("data_completeness") not in (None, "complete"):
        return True
    if any(row.get(field) is None for field in required):
        return True
    meta = _raw_payload_meta(row)
    raw_value = meta.get("row")
    raw: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
    if meta.get("status") in _INCOMPLETE_STATES or raw.get("status") in _INCOMPLETE_STATES:
        return True
    return any(field in raw and raw[field] is None for field in required)


def holding_return_snapshot_table_exists(cur) -> bool:
    """Return whether the optional holding-return snapshot table is installed."""
    cur.execute("SELECT to_regclass('public.valuation_results') AS table_name")
    row = cur.fetchone()
    if not row:
        return False
    value = row.get("table_name") if isinstance(row, dict) else row[0]
    return bool(value)


def _row_date(row: dict[str, Any]) -> Date:
    raw = row.get("snapshot_date") or row.get("date")
    if isinstance(raw, Date):
        return raw
    return Date.fromisoformat(str(raw))


def _sample_snapshot_rows(rows: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    if interval != "weekly" or len(rows) <= 2:
        return rows

    sampled: list[dict[str, Any]] = []
    last_date: Date | None = None
    for row in rows:
        current = _row_date(row)
        if last_date is None or (current - last_date).days >= 7:
            sampled.append(row)
            last_date = current
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def _snapshot_today_window(rows: list[dict[str, Any]], today_label_date: Date | None) -> dict[str, Any] | None:
    if not rows:
        return None
    latest = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    latest_date = _row_date(latest)
    if (latest.get("data_completeness") not in (None, "complete")
            or latest.get("market_value") is None
            or (latest.get("total_pnl") is None and latest.get("cumulative_capital") is None and latest.get("cost") is None)
            or (previous is not None and (previous.get("market_value") is None
                or (previous.get("total_pnl") is None and previous.get("cumulative_capital") is None and previous.get("cost") is None)))):
        return {"label_date": (today_label_date or latest_date).isoformat(),
                "baseline_date": _row_date(previous).isoformat() if previous else None,
                "latest_date": latest_date.isoformat(), "total_return": None, "return_rate": None,
                "status": "unavailable", "reason": "snapshot_evidence_incomplete"}
    if today_label_date is not None and latest_date < today_label_date:
        return {
            "label_date": today_label_date.isoformat(),
            "baseline_date": latest_date.isoformat(),
            "latest_date": latest_date.isoformat(),
            "total_return": None,
            "return_rate": None,
            "status": "unavailable",
            "reason": "stale_snapshot",
        }

    previous_date = _row_date(previous) if previous else None
    latest_return = _snapshot_return_amount(latest)
    previous_return = _snapshot_return_amount(previous) if previous else Decimal("0")
    base = _snapshot_decimal(previous if previous is not None else latest, "market_value")
    if latest_return is None or previous_return is None or base is None:
        return {"label_date": (today_label_date or latest_date).isoformat(),
                "baseline_date": previous_date.isoformat() if previous_date else None,
                "latest_date": latest_date.isoformat(), "total_return": None, "return_rate": None,
                "status": "unavailable", "reason": "snapshot_evidence_incomplete"}
    total_return = latest_return - previous_return
    denominator = abs(base)
    return {
        "label_date": (today_label_date or latest_date).isoformat(),
        "baseline_date": previous_date.isoformat() if previous_date else None,
        "latest_date": latest_date.isoformat(),
        "total_return": norm(total_return),
        "return_rate": norm(total_return / denominator) if denominator > Decimal("0.0001") else None,
        **({"status": "partial", "reason": "zero_return_denominator"} if denominator <= Decimal("0.0001") else {}),
    }


def _snapshot_return_amount(row: dict[str, Any] | None) -> Decimal | None:
    if not row:
        return None
    raw = _raw_payload_row(row)
    # An explicitly unknown total is not equivalent to a section that never
    # supplied a total and can be reconstructed from its proven inputs.
    if "total_pnl" in raw and raw["total_pnl"] is None:
        return None
    total = _snapshot_decimal(row, "total_pnl")
    if total is not None:
        return total
    capital = _snapshot_decimal(row, "cumulative_capital")
    if capital is None:
        capital = _snapshot_decimal(row, "cost")
    market = _snapshot_decimal(row, "market_value")
    return market - capital if market is not None and capital is not None else None


def _a_stock_snapshot_has_invalid_zero_value_rows(rows: list[dict[str, Any]]) -> bool:
    """Reject polluted A-share snapshots that lost the cash/equity history."""
    eps = Decimal("0.0001")
    has_positive_value = any(abs(dec(row.get("market_value"))) > eps for row in rows)
    if not has_positive_value:
        return False
    return any(
        abs(dec(row.get("market_value"))) <= eps
        and abs(dec(row.get("cumulative_capital"))) > eps
        and dec(row.get("display_return_rate") or row.get("time_weighted_return_rate")) <= Decimal("-0.99")
        for row in rows
    )


def _raw_payload_meta(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _stored_today_window_for_label(row: dict[str, Any], today_label_date: Date | None) -> dict[str, Any] | None:
    stored_today_window = _raw_payload_meta(row).get("today_window")
    if not isinstance(stored_today_window, dict):
        return None
    try:
        stored_latest_date = Date.fromisoformat(str(stored_today_window.get("latest_date")))
    except (TypeError, ValueError):
        stored_latest_date = None
    if stored_latest_date is not None and stored_latest_date > _row_date(row):
        return None
    if today_label_date is None:
        return stored_today_window
    if (_row_date(row) >= today_label_date
            and str(stored_today_window.get("label_date") or "") == today_label_date.isoformat()):
        return stored_today_window
    return None


def _raw_payload_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = _raw_payload_meta(row).get("row")
    return raw if isinstance(raw, dict) else {}


def _snapshot_decimal(row: dict[str, Any], field: str) -> Decimal | None:
    # Earlier writers sometimes persisted zero for an explicit unknown. The
    # retained original row is evidence of that missing value, not a zero.
    raw = _raw_payload_row(row)
    if field in raw and raw[field] is None:
        return None
    return _nullable_decimal(row.get(field))


def _snapshot_norm(row: dict[str, Any], field: str) -> Any:
    number = _snapshot_decimal(row, field)
    return norm(number) if number is not None else None


def _snapshot_note() -> list[dict[str, str]]:
    return [
        {
            "account_code": "snapshot",
            "label": "holding return snapshot",
            "reason": "Return chart reads precomputed holding-return snapshots instead of rebuilding historical returns on page request.",
        }
    ]


def _total_today_window_from_section_snapshots(
    cur,
    *,
    base_currency: str,
    view_as_of: Date,
    today_label_date: Date | None,
) -> dict[str, Any] | None:
    total_return = Decimal("0")
    denominator = Decimal("0")
    latest_dates: list[Date] = []
    baseline_dates: list[Date] = []
    has_window = False

    for section in sorted(SNAPSHOT_SECTIONS - {SNAPSHOT_SECTION_TOTAL}):
        cur.execute(
            """
            SELECT
                snapshot_date,
                market_value,
                cost,
                total_pnl,
                cumulative_capital,
                net_flow,
                capital_return_rate,
                daily_return_rate,
                time_weighted_return_rate,
                display_return_rate,
                data_completeness,
                raw_json
            FROM valuation_results
            WHERE result_type = 'holding_return' AND section = %s
              AND base_currency = %s
              AND snapshot_date <= %s
            ORDER BY snapshot_date
            """,
            (section, base_currency, view_as_of),
        )
        rows = [dict(row) for row in cur.fetchall()]
        if not rows:
            continue
        latest = rows[-1]
        if (_incomplete_snapshot_row(latest, _SECTION_REQUIRED)
                or (today_label_date is not None and _row_date(latest) < today_label_date)):
            return {"label_date": (today_label_date or view_as_of).isoformat(),
                    "baseline_date": None, "latest_date": _row_date(latest).isoformat(),
                    "total_return": None, "return_rate": None,
                    "status": "unavailable", "reason": "section_snapshot_evidence_incomplete"}
        window = _stored_today_window_for_label(latest, today_label_date) or _snapshot_today_window(
            rows,
            today_label_date,
        )
        if not window:
            continue
        if window.get("total_return") is None or window.get("return_rate") is None:
            return {**window, "status": "unavailable"}
        has_window = True
        total_return += dec(window.get("total_return"))
        denominator += abs(dec(latest.get("market_value")))
        try:
            latest_dates.append(Date.fromisoformat(str(window.get("latest_date"))))
        except (TypeError, ValueError):
            pass
        try:
            baseline_dates.append(Date.fromisoformat(str(window.get("baseline_date"))))
        except (TypeError, ValueError):
            pass

    if not has_window:
        return None
    latest_date = max(latest_dates) if latest_dates else view_as_of
    baseline_date = max(baseline_dates) if baseline_dates else None
    return {
        "label_date": (today_label_date or latest_date).isoformat(),
        "baseline_date": baseline_date.isoformat() if baseline_date else None,
        "latest_date": latest_date.isoformat(),
        "total_return": norm(total_return),
        "return_rate": norm(total_return / denominator if denominator > Decimal("0.0001") else Decimal("0")),
    }


def build_investment_return_series_payload_from_snapshot(
    cur,
    *,
    start_date: Date,
    interval: str,
    base_currency: str,
    view_as_of: Date,
    today_label_date: Date | None = None,
    require_data_completeness: bool = False,
) -> dict[str, Any] | None:
    """Read the total investment return series from precomputed snapshots."""
    if not holding_return_snapshot_table_exists(cur):
        return None

    cur.execute(
        """
        SELECT
            snapshot_date,
            market_value,
            cost,
            position_pnl,
            realized_income,
            total_pnl,
            return_rate,
            daily_return_rate,
            time_weighted_return_rate,
            data_completeness,
            raw_json
        FROM valuation_results
        WHERE result_type = 'holding_return' AND section = %s
          AND base_currency = %s
          AND snapshot_date >= %s
          AND snapshot_date <= %s
        ORDER BY snapshot_date
        """,
        (SNAPSHOT_SECTION_TOTAL, base_currency, start_date, view_as_of),
    )
    raw_rows = [dict(row) for row in cur.fetchall()]
    if not raw_rows:
        return None
    if _row_date(raw_rows[-1]) < view_as_of:
        return None
    if require_data_completeness and any(
        row.get("data_completeness") != "complete" or _incomplete_snapshot_row(row, _TOTAL_REQUIRED)
        for row in raw_rows
    ):
        return None

    rows = _sample_snapshot_rows(raw_rows, interval)
    payload_rows = [
        {
            "date": norm(_row_date(row)),
            "market_value": _snapshot_norm(row, "market_value"),
            "cost": _snapshot_norm(row, "cost"),
            "position_pnl": _snapshot_norm(row, "position_pnl"),
            "realized_income": _snapshot_norm(row, "realized_income"),
            "total_pnl": _snapshot_norm(row, "total_pnl"),
            "return_rate": _snapshot_norm(row, "return_rate"),
            "reported_return_adjustment": (
                _nullable_norm(_raw_payload_row(row)["reported_return_adjustment"])
                if "reported_return_adjustment" in _raw_payload_row(row)
                else None if _incomplete_snapshot_row(row, _TOTAL_REQUIRED) else "0"
            ),
            "daily_return_rate": _snapshot_norm(row, "daily_return_rate"),
            "time_weighted_return_rate": _snapshot_norm(row, "time_weighted_return_rate"),
        }
        for row in rows
    ]
    payload = {
        "start": _row_date(rows[0]).isoformat(),
        "end": _row_date(rows[-1]).isoformat(),
        "base_currency": base_currency,
        "interval": interval,
        "method": SNAPSHOT_METHOD,
        "coverage_notes": _raw_payload_meta(rows[-1]).get("coverage_notes") or _snapshot_note(),
        "rows": payload_rows,
    }
    incomplete = any(_incomplete_snapshot_row(row, _TOTAL_REQUIRED) for row in raw_rows)
    if incomplete:
        meta = _raw_payload_meta(raw_rows[-1])
        payload.update(status=meta.get("status") or "unavailable", reason=meta.get("reason") or "snapshot_evidence_incomplete",
                       data_completeness="partial")
    if today_label_date is not None:
        if incomplete:
            payload["today_window"] = {"label_date": today_label_date.isoformat(),
                                       "baseline_date": None, "latest_date": _row_date(rows[-1]).isoformat(),
                                       "total_return": None, "return_rate": None,
                                       "status": "unavailable", "reason": "snapshot_evidence_incomplete"}
        else:
            live_total_today = _total_today_window_from_section_snapshots(
                cur, base_currency=base_currency, view_as_of=view_as_of, today_label_date=today_label_date,
            )
            stored_today_window = _stored_today_window_for_label(rows[-1], today_label_date)
            payload["today_window"] = (live_total_today if live_total_today is not None else
                                       stored_today_window if stored_today_window is not None else
                                       _snapshot_today_window(rows, today_label_date))
    return payload


def build_section_return_series_payload_from_snapshot(
    cur,
    *,
    section: str,
    start_date: Date,
    interval: str,
    base_currency: str,
    view_as_of: Date,
    today_label_date: Date | None = None,
    require_data_completeness: bool = False,
) -> dict[str, Any] | None:
    """Read a section return-rate series from precomputed snapshots."""
    if section not in SNAPSHOT_SECTIONS or section == SNAPSHOT_SECTION_TOTAL:
        return None
    if not holding_return_snapshot_table_exists(cur):
        return None

    cur.execute(
        """
        SELECT
            snapshot_date,
            market_value,
            net_flow,
            cumulative_capital,
            capital_return_rate,
            daily_return_rate,
            time_weighted_return_rate,
            display_return_rate,
            data_completeness,
            raw_json
        FROM valuation_results
        WHERE result_type = 'holding_return' AND section = %s
          AND base_currency = %s
          AND snapshot_date >= %s
          AND snapshot_date <= %s
        ORDER BY snapshot_date
        """,
        (section, base_currency, start_date, view_as_of),
    )
    raw_rows = [dict(row) for row in cur.fetchall()]
    if not raw_rows:
        return None
    if _row_date(raw_rows[-1]) < view_as_of:
        return None
    if require_data_completeness and any(
        row.get("data_completeness") != "complete" or _incomplete_snapshot_row(row, _SECTION_REQUIRED)
        for row in raw_rows
    ):
        return None
    if section == "a_stock" and _a_stock_snapshot_has_invalid_zero_value_rows(raw_rows):
        return None

    rows = _sample_snapshot_rows(raw_rows, interval)
    latest = rows[-1]
    latest_meta = _raw_payload_meta(latest)
    latest_summary = latest_meta.get("summary") if isinstance(latest_meta.get("summary"), dict) else None
    latest_return = _snapshot_return_amount(latest)
    latest_rate = _snapshot_decimal(latest, "display_return_rate")
    summary = latest_summary or {
        "total_return": norm(latest_return) if not _incomplete_snapshot_row(latest, _SECTION_REQUIRED) else None,
        "total_return_rate": norm(latest_rate) if latest_rate is not None else None,
        "annualized_return_rate": norm(annualized_rate(latest_rate, _row_date(rows[0]), _row_date(latest)))
                                  if latest_rate is not None else None,
    }

    payload_rows = []
    for row in rows:
        raw_row = _raw_payload_row(row)
        display_rate = _snapshot_decimal(row, "display_return_rate")
        payload_rows.append(
            {
                "date": norm(_row_date(row)),
                "market_value": _snapshot_norm(row, "market_value"),
                "net_flow": _snapshot_norm(row, "net_flow"),
                "cumulative_capital": _snapshot_norm(row, "cumulative_capital"),
                "capital_return_rate": _snapshot_norm(row, "capital_return_rate"),
                "daily_return_rate": _snapshot_norm(row, "daily_return_rate"),
                "time_weighted_return_rate": _snapshot_norm(row, "time_weighted_return_rate"),
                "display_return_rate": norm(display_rate),
                "display_return_rate_raw": norm(_field(raw_row, "display_return_rate_raw", display_rate)),
            }
        )

    payload = {
        "section": section,
        "label": latest_meta.get("label") or section,
        "start": _row_date(rows[0]).isoformat(),
        "end": _row_date(rows[-1]).isoformat(),
        "base_currency": base_currency,
        "interval": interval,
        "method": SNAPSHOT_METHOD,
        "coverage_notes": latest_meta.get("coverage_notes") or _snapshot_note(),
        "summary": summary,
        "rows": payload_rows,
    }
    incomplete = any(_incomplete_snapshot_row(row, _SECTION_REQUIRED) for row in raw_rows)
    if incomplete:
        payload.update(status=latest_meta.get("status") or "unavailable",
                       reason=latest_meta.get("reason") or "snapshot_evidence_incomplete", data_completeness="partial",
                       summary={"total_return": None, "total_return_rate": None, "annualized_return_rate": None})
    if today_label_date is not None:
        if incomplete:
            payload["today_window"] = {"label_date": today_label_date.isoformat(),
                                       "baseline_date": None, "latest_date": _row_date(rows[-1]).isoformat(),
                                       "total_return": None, "return_rate": None,
                                       "status": "unavailable", "reason": "snapshot_evidence_incomplete"}
        else:
            stored_today_window = _stored_today_window_for_label(rows[-1], today_label_date)
            payload["today_window"] = (stored_today_window if stored_today_window is not None
                                       else _snapshot_today_window(rows, today_label_date))
    return payload


def _snapshot_source_hash(section: str, base_currency: str, row: dict[str, Any]) -> str:
    payload = {
        "section": section,
        "base_currency": base_currency,
        "row": row,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upsert_investment_return_series_snapshot(
    cur,
    *,
    payload: dict[str, Any],
    base_currency: str,
    section: str = SNAPSHOT_SECTION_TOTAL,
) -> int:
    """Persist a computed return-series payload for future fast reads."""
    if not holding_return_snapshot_table_exists(cur):
        return 0

    rows = payload.get("rows") or []
    payload_meta = {
        "summary": payload.get("summary"),
        "coverage_notes": payload.get("coverage_notes"),
        "label": payload.get("label"),
        "method": payload.get("method"),
        "today_window": payload.get("today_window"),
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "data_completeness": payload.get("data_completeness"),
    }
    rows_count = len(rows)
    holdings_count = int(payload.get("holdings_count") or rows_count)
    accounts_count = int(payload.get("accounts_count") or 0)
    declared_completeness = payload.get("data_completeness")
    required = _TOTAL_REQUIRED if section == SNAPSHOT_SECTION_TOTAL else _SECTION_REQUIRED
    written = 0
    for row in rows:
        if not row.get("date"):
            continue
        snapshot_date = _row_date(row)
        market_value = _field(row, "market_value")
        cost = _field(row, "cost", row.get("cumulative_capital"))
        position_pnl = _field(row, "position_pnl")
        realized_income = _field(row, "realized_income")
        total_pnl = _field(row, "total_pnl")
        return_rate = _field(row, "return_rate")
        daily_return_rate = _field(row, "daily_return_rate")
        twr = _field(row, "time_weighted_return_rate", row.get("display_return_rate"))
        incomplete = (declared_completeness not in (None, "complete")
                      or payload.get("status") in _INCOMPLETE_STATES
                      or row.get("status") in _INCOMPLETE_STATES
                      or any(row.get(field) is None for field in required))
        data_completeness = "partial" if incomplete else "complete"
        source_hash = _snapshot_source_hash(section, base_currency, row)
        raw_json = json.dumps({**payload_meta, "row": row}, ensure_ascii=False, sort_keys=True, default=str)
        cur.execute(
            """
            INSERT INTO valuation_results (
                result_type,
                snapshot_date,
                section,
                base_currency,
                market_value,
                cost,
                position_pnl,
                realized_income,
                total_pnl,
                net_flow,
                cumulative_capital,
                return_rate,
                daily_return_rate,
                time_weighted_return_rate,
                capital_return_rate,
                display_return_rate,
                rows_count,
                source,
                source_hash,
                raw_json,
                holdings_count,
                accounts_count,
                data_completeness,
                updated_at
            )
            VALUES (
                'holding_return', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s, NOW()
            )
            ON CONFLICT (snapshot_date, section, base_currency) WHERE result_type='holding_return' DO UPDATE SET
                market_value = EXCLUDED.market_value,
                cost = EXCLUDED.cost,
                position_pnl = EXCLUDED.position_pnl,
                realized_income = EXCLUDED.realized_income,
                total_pnl = EXCLUDED.total_pnl,
                net_flow = EXCLUDED.net_flow,
                cumulative_capital = EXCLUDED.cumulative_capital,
                return_rate = EXCLUDED.return_rate,
                daily_return_rate = EXCLUDED.daily_return_rate,
                time_weighted_return_rate = EXCLUDED.time_weighted_return_rate,
                capital_return_rate = EXCLUDED.capital_return_rate,
                display_return_rate = EXCLUDED.display_return_rate,
                rows_count = EXCLUDED.rows_count,
                source = EXCLUDED.source,
                source_hash = EXCLUDED.source_hash,
                raw_json = EXCLUDED.raw_json,
                holdings_count = EXCLUDED.holdings_count,
                accounts_count = EXCLUDED.accounts_count,
                data_completeness = EXCLUDED.data_completeness,
                updated_at = NOW()
            """,
            (
                snapshot_date,
                section,
                base_currency,
                market_value,
                cost,
                position_pnl,
                realized_income,
                total_pnl,
                _field(row, "net_flow"),
                _field(row, "cumulative_capital", cost),
                return_rate,
                daily_return_rate,
                twr,
                _field(row, "capital_return_rate", return_rate),
                _field(row, "display_return_rate", twr if twr is not None else return_rate),
                rows_count,
                SNAPSHOT_METHOD,
                source_hash,
                raw_json,
                holdings_count,
                accounts_count,
                data_completeness,
            ),
        )
        written += 1
    return written
