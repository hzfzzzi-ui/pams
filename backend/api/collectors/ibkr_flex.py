"""IBKR Flex Web Service collector for local PAMS holdings snapshots.

This module only reads IBKR Flex reports. It does not store broker passwords,
does not request trading permissions, and does not submit orders.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2.extras
import requests
from domain.assets.alias_policy import identity_alias_sql
from domain.brokers.ibkr import ibkr_report_state as report_state
from domain.brokers.ibkr import ibkr_cash_events

FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SYNC_SOURCE = "ibkr_flex"
USER_AGENT = "PAMS-IBKR-Flex/1.0"


class IBKRFlexError(RuntimeError):
    """Raised when an IBKR Flex request, parse, or persistence step fails."""


@dataclass(frozen=True)
class FlexParseResult:
    """Normalized holdings parsed from one IBKR Flex XML report."""

    rows: list[dict[str, Any]]
    statement_as_of: date
    source_account_ids: list[str]
    trades: list[dict[str, Any]] = field(default_factory=list)
    history_rows: list[dict[str, Any]] = field(default_factory=list)
    cash_movements: list[dict[str, Any]] = field(default_factory=list)
    cash_events: list[dict[str, Any]] = field(default_factory=list)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(root: ET.Element, name: str) -> str | None:
    wanted = name.lower()
    for child in root.iter():
        if _local_name(child.tag).lower() == wanted:
            return (child.text or "").strip()
    return None


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    text = str(value).strip()
    if not text or text in {"--", "N/A", "null", "None"}:
        return default
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    try:
        number = float(text)
    except ValueError:
        return default
    return -number if negative else number


def _num_from(attrs: dict[str, str], keys: list[str], default: float | None = None) -> float | None:
    lowered = {k.lower(): v for k, v in attrs.items()}
    for key in keys:
        value = _num(lowered.get(key.lower()), None)
        if value is not None:
            return value
    return default


def _attr(attrs: dict[str, str], *keys: str, default: str | None = None) -> str | None:
    lowered = {k.lower(): v for k, v in attrs.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _parse_ib_date(value: Any) -> date | None:
    """Parse common IBKR Flex date formats into a Python date."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidates = [
        "%Y%m%d",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d %B, %Y",
        "%d %b, %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if match:
        year, month, day = (int(x) for x in match.groups())
        return date(year, month, day)
    match = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", text)
    if match:
        year, month, day = (int(x) for x in match.groups())
        return date(year, month, day)
    return None


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table_name}",))
        row = cur.fetchone()
    return bool(row and row[0])


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
            )
            """,
            (table_name, column_name),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def parse_flex_send_response(xml_text: str) -> dict[str, str | None]:
    """Parse `/SendRequest` XML and return the reference code and pickup URL."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise IBKRFlexError(f"Invalid IBKR SendRequest XML: {exc}") from exc

    status = _child_text(root, "Status")
    if status != "Success":
        code = _child_text(root, "ErrorCode")
        message = _child_text(root, "ErrorMessage") or "IBKR Flex SendRequest failed"
        raise IBKRFlexError(f"IBKR Flex SendRequest failed: {code or 'unknown'} {message}")

    reference_code = _child_text(root, "ReferenceCode")
    if not reference_code:
        raise IBKRFlexError("IBKR Flex SendRequest did not return ReferenceCode")

    return {
        "status": status,
        "reference_code": reference_code,
        "url": _child_text(root, "url"),
    }


def request_flex_reference(
    token: str,
    query_id: str,
    *,
    base_url: str = FLEX_BASE_URL,
    flex_version: int = 3,
    timeout: int = 30,
) -> dict[str, str | None]:
    """Request generation of a configured Flex Query and return its reference code."""
    from domain.imports.isolation import require_external_access
    require_external_access()
    response = requests.get(
        f"{base_url.rstrip('/')}/SendRequest",
        params={"t": token, "q": query_id, "v": flex_version},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_flex_send_response(response.text)


def _is_generation_pending(xml_text: str) -> tuple[bool, str | None]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False, None
    if _local_name(root.tag).lower() != "flexstatementresponse":
        return False, None
    status = _child_text(root, "Status")
    if status == "Success":
        return False, None
    message = _child_text(root, "ErrorMessage") or ""
    code = _child_text(root, "ErrorCode")
    pending = "generat" in message.lower() or "progress" in message.lower()
    return pending, f"{code or 'unknown'} {message}".strip()


def _contains_tag(xml_text: str, tag_name: str) -> bool:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False
    wanted = tag_name.lower()
    return any(_local_name(elem.tag).lower() == wanted for elem in root.iter())


def fetch_flex_statement(
    token: str,
    reference_code: str,
    *,
    pickup_url: str | None = None,
    base_url: str = FLEX_BASE_URL,
    flex_version: int = 3,
    timeout: int = 60,
    attempts: int = 6,
    wait_seconds: float = 5.0,
) -> str:
    """Fetch the generated Flex XML report, retrying while IBKR is still preparing it."""
    from domain.imports.isolation import require_external_access
    require_external_access()
    url = pickup_url or f"{base_url.rstrip('/')}/GetStatement"
    last_pending: str | None = None
    for attempt in range(1, attempts + 1):
        response = requests.get(
            url,
            params={"t": token, "q": reference_code, "v": flex_version},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        response.raise_for_status()
        pending, message = _is_generation_pending(response.text)
        if not pending:
            return response.text
        last_pending = message
        if attempt < attempts:
            time.sleep(wait_seconds)
    raise IBKRFlexError(f"IBKR Flex statement was not ready after {attempts} attempts: {last_pending}")


def _safe_name(value: str, max_len: int = 180) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text[:max_len] if len(text) > max_len else text


def _position_row(elem: ET.Element, account_code: str, fallback_as_of: date) -> dict[str, Any] | None:
    attrs = {str(k): str(v).strip() for k, v in elem.attrib.items()}
    symbol = (_attr(attrs, "symbol", "underlyingSymbol", "securityID", "conid", default="") or "").upper()
    description = _attr(attrs, "description", "assetDescription", "name", default=symbol) or symbol
    currency = (_attr(attrs, "currency", "reportingCurrency", default="USD") or "USD").upper()
    quantity = _num_from(attrs, ["position", "quantity", "qty"], 0.0) or 0.0
    multiplier = _num_from(attrs, ["multiplier"], 1.0) or 1.0
    current_price = _num_from(attrs, ["markPrice", "closePrice", "price"], None)
    market_value = _num_from(attrs, ["marketValue", "positionValue", "value", "markValue"], None)
    if market_value is None and current_price is not None:
        market_value = quantity * current_price * multiplier
    if market_value is None:
        market_value = 0.0

    if abs(quantity) < 1e-12 and abs(market_value) < 1e-8:
        return None

    cost = _num_from(attrs, ["costBasisMoney", "costBasis", "cost"], None)
    buy_price = _num_from(attrs, ["costBasisPrice", "avgCost", "averageCost"], None)
    if buy_price is None and cost is not None and abs(quantity * multiplier) > 1e-12:
        buy_price = cost / (quantity * multiplier)
    if current_price is None and abs(quantity * multiplier) > 1e-12:
        current_price = market_value / (quantity * multiplier)

    if not symbol:
        symbol = _safe_name(description).upper()
    name = _safe_name(description if description.upper() == symbol else f"{symbol} - {description}")

    row_as_of = _parse_ib_date(_attr(attrs, "reportDate", "date")) or fallback_as_of
    # Use the explicitly reported contract type; retain the existing stock path.
    from domain.brokers.ibkr.ibkr_instrument_types import SECURITY_LABELS
    contract_type = _attr(attrs, "assetCategory", default="")
    minor = SECURITY_LABELS.get(contract_type)
    if minor:
        major_category, minor_category = "其他", minor
    elif contract_type == "STK":
        major_category, minor_category = "美股", "美股基金"
    else:
        major_category, minor_category = "其他", "待分类"
    return {
        "source_account_id": _attr(attrs, "accountId", "acctId", default=None),
        "symbol": symbol,
        "name": name,
        "asset_liability": "资产" if market_value >= 0 else "负债",
        "currency": currency,
        "account_code": account_code,
        "major_category": major_category,
        "minor_category": minor_category,
        "market_value": market_value,
        "cost": cost,
        "quantity": quantity,
        "buy_price": buy_price,
        "current_price": current_price,
        "as_of": row_as_of,
    }


def _cash_row(elem: ET.Element, account_code: str, fallback_as_of: date) -> dict[str, Any] | None:
    attrs = {str(k): str(v).strip() for k, v in elem.attrib.items()}
    currency = (_attr(attrs, "currency", "reportCurrency", default="") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency or ""):
        return None
    cash = _num_from(
        attrs,
        ["endingCash", "endingSettledCash", "totalCash", "settledCash", "cash"],
        None,
    )
    if cash is None:
        return None
    if cash == 0:
        # Only an explicit report-date ending balance can prove a zero cash member.
        from decimal import Decimal, InvalidOperation
        values=[v for key,v in attrs.items() if key.lower()=='endingcash']
        try:
            if (len(values)!=1 or not Decimal(values[0]).is_finite() or Decimal(values[0])!=0
                    or _parse_ib_date(_attr(attrs, 'toDate'))!=fallback_as_of):
                return None
        except (InvalidOperation,ValueError):
            return None

    return {
        "source_account_id": _attr(attrs, "accountId", "acctId", default=None),
        "symbol": f"CASH_{currency}",
        "name": f"Cash {currency}",
        "asset_liability": "资产" if cash >= 0 else "负债",
        "currency": currency,
        "account_code": account_code,
        "major_category": "现金" if cash >= 0 else "借款",
        "minor_category": "现金/活期" if cash >= 0 else "借款",
        "market_value": cash,
        "cost": cash,
        "quantity": cash,
        "buy_price": 1.0,
        "current_price": 1.0,
        "as_of": fallback_as_of,
    }


def _cash_movement_row(elem: ET.Element, account_code: str, fallback_as_of: date) -> dict[str, Any] | None:
    attrs = {str(k): str(v).strip() for k, v in elem.attrib.items()}
    currency = (_attr(attrs, "currency", "reportCurrency", default="") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency or ""):
        return None

    deposits = _num_from(attrs, ["deposits"], None)
    withdrawals = _num_from(attrs, ["withdrawals"], None)
    if deposits is None and withdrawals is None:
        deposit_withdrawals = _num_from(attrs, ["depositWithdrawals"], 0.0) or 0.0
        deposits = deposit_withdrawals if deposit_withdrawals > 0 else 0.0
        withdrawals = abs(deposit_withdrawals) if deposit_withdrawals < 0 else 0.0
    deposits = deposits or 0.0
    withdrawals = withdrawals or 0.0
    if abs(deposits) < 1e-8 and abs(withdrawals) < 1e-8:
        return None

    movement_type = "deposit" if deposits > 0 else "withdrawal"
    amount = deposits if deposits > 0 else -abs(withdrawals)
    movement_date = _parse_ib_date(_attr(attrs, "toDate", "reportDate", "date")) or fallback_as_of
    source_account_id = _attr(attrs, "accountId", "acctId", default=None)
    return {
        "account_code": account_code,
        "source_account_id": source_account_id,
        "movement_date": movement_date,
        "currency": currency,
        "movement_type": movement_type,
        "amount": amount,
        "source_key": f"{source_account_id or account_code}:{movement_date.isoformat()}:{currency}:{movement_type}:{amount}",
        "raw": attrs,
    }


def _trade_row(elem: ET.Element, account_code: str, fallback_as_of: date) -> dict[str, Any] | None:
    attrs = {str(k): str(v).strip() for k, v in elem.attrib.items()}
    symbol = (_attr(attrs, "symbol", "underlyingSymbol", "securityID", "conid", default="") or "").upper()
    description = _attr(attrs, "description", "assetDescription", "name", default=symbol) or symbol
    trade_date = _parse_ib_date(_attr(attrs, "tradeDate", "reportDate", "date")) or fallback_as_of
    quantity = _num_from(attrs, ["quantity", "tradeQuantity", "qty"], 0.0) or 0.0
    price = _num_from(attrs, ["tradePrice", "price"], None)
    net_cash = _num_from(attrs, ["netCash", "amount"], None)
    gross_amount = _num_from(attrs, ["tradeMoney", "tradeGross", "proceeds"], None)
    commission = _num_from(attrs, ["ibCommission", "tradeCommission", "commission"], 0.0) or 0.0
    realized_pnl = _num_from(attrs, ["fifoPnlRealized", "realizedPnl"], 0.0) or 0.0
    side = (_attr(attrs, "buySell", "side", default="") or "").upper()
    if not symbol or not trade_date or (abs(quantity) < 1e-12 and net_cash is None and gross_amount is None):
        return None

    key_parts = [
        _attr(attrs, "accountId", "acctId", default="") or "",
        _attr(attrs, "tradeID", "transactionID", "ibExecID", default="") or "",
        trade_date.isoformat(),
        symbol,
        side,
        str(quantity),
        str(price),
        str(net_cash),
    ]
    source_trade_key = hashlib.sha256("|".join(key_parts).encode("utf-8")).hexdigest()

    return {
        "account_code": account_code,
        "source_account_id": _attr(attrs, "accountId", "acctId", default=None),
        "source_trade_key": source_trade_key,
        "trade_id": _attr(attrs, "tradeID", default=None),
        "transaction_id": _attr(attrs, "transactionID", default=None),
        "order_id": _attr(attrs, "ibOrderID", "orderID", default=None),
        "symbol": symbol,
        "name": _safe_name(description if description.upper() == symbol else f"{symbol} - {description}"),
        "currency": (_attr(attrs, "currency", "reportingCurrency", default="USD") or "USD").upper(),
        "asset_category": _attr(attrs, "assetCategory", default=None),
        "trade_date": trade_date,
        "side": side or None,
        "quantity": quantity,
        "price": price,
        "gross_amount": gross_amount,
        "commission": commission,
        "net_cash": net_cash,
        "realized_pnl": realized_pnl,
        "raw": attrs,
    }


def parse_flex_report(xml_text: str, *, account_code: str = "ibkr", allow_empty_holdings: bool = False) -> FlexParseResult:
    """Parse IBKR Flex XML into PAMS holdings rows."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise IBKRFlexError(f"Invalid IBKR Flex report XML: {exc}") from exc

    source_accounts: set[str] = set()
    history_rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    cash_movements: list[dict[str, Any]] = []
    cash_events: list[dict[str, Any]] = []
    parents = {child: parent for parent in root.iter() for child in parent}

    statements = [elem for elem in root.iter() if _local_name(elem.tag).lower() == "flexstatement"]
    if not statements:
        statements = [root]

    for statement in statements:
        attrs = {str(k): str(v).strip() for k, v in statement.attrib.items()}
        statement_date = None
        for key in ("toDate", "reportDate", "whenGenerated", "fromDate"):
            parsed = _parse_ib_date(_attr(attrs, key, default=None))
            if parsed:
                statement_date = parsed
                break
        fallback_as_of = statement_date or date.today()
        acct = _attr(attrs, "accountId", "acctId", default=None)
        if acct:
            source_accounts.add(acct)

        for elem in statement.iter():
            local = _local_name(elem.tag).lower()
            if local == "openposition":
                row = _position_row(elem, account_code, fallback_as_of)
                if row:
                    history_rows.append(row)
                    if row.get("source_account_id"):
                        source_accounts.add(str(row["source_account_id"]))
            elif local in {"cashreportcurrency", "cashbalance"}:
                row = _cash_row(elem, account_code, fallback_as_of)
                if row:
                    history_rows.append(row)
                    if row.get("source_account_id"):
                        source_accounts.add(str(row["source_account_id"]))
                movement = _cash_movement_row(elem, account_code, fallback_as_of)
                if movement:
                    cash_movements.append(movement)
                    if movement.get("source_account_id"):
                        source_accounts.add(str(movement["source_account_id"]))
            elif local == "trade":
                trade = _trade_row(elem, account_code, fallback_as_of)
                if trade:
                    trades.append(trade)
                    if trade.get("source_account_id"):
                        source_accounts.add(str(trade["source_account_id"]))
            elif local == "cashtransaction":
                envelope: dict[str, Any] = {"raw": dict(elem.attrib), "statement_account_id": acct}
                ancestor: ET.Element | None = elem
                while ancestor is not None:
                    if ancestor.get("accountId") not in (None, "", acct) or ancestor.get("model"):
                        envelope["scope_error"] = True
                    ancestor = parents.get(ancestor)
                cash_events.append(envelope)

    if not history_rows:
        if not allow_empty_holdings:
            raise IBKRFlexError("No holdings or cash rows were parsed from the IBKR Flex report")
        statement_dates=[_parse_ib_date(statement.get('toDate')) for statement in statements]
        statement_dates=[day for day in statement_dates if day is not None]
        if not statement_dates:
            raise IBKRFlexError('No explicit statement date in IBKR cash report')
        return FlexParseResult(rows=[],statement_as_of=max(statement_dates),source_account_ids=sorted(source_accounts),
            trades=trades,history_rows=[],cash_movements=cash_movements,cash_events=cash_events)

    row_dates = [r["as_of"] for r in history_rows if isinstance(r.get("as_of"), date)]
    statement_as_of = max(row_dates)
    rows = [dict(row) for row in history_rows if row.get("as_of") == statement_as_of]
    if not rows:
        raise IBKRFlexError(f"No current rows were parsed for latest IBKR statement date {statement_as_of}")

    return FlexParseResult(
        rows=rows,
        statement_as_of=statement_as_of,
        source_account_ids=sorted(source_accounts),
        trades=trades,
        history_rows=history_rows,
        cash_movements=cash_movements,
        cash_events=cash_events,
    )


def default_data_dir() -> Path:
    """Return the persistent local data directory used by the API container."""
    return Path(os.environ.get("PAMS_DATA_DIR", "/app/data"))


def _require_schema_table(conn, table: str) -> None:
    """Require provisioned schema; never run DDL from a sync request."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", ("public." + table,))
        if not cur.fetchone()[0]:
            raise IBKRFlexError(f"Required IBKR schema table is missing: {table}")


def ensure_sync_table(conn) -> None:
    _require_schema_table(conn, "broker_processing_state")


def _latest_portfolio_as_of(conn) -> date | None:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(as_of) FROM pams_holdings_snapshot")
        value = cur.fetchone()[0]
    return value


def _target_as_of(
    conn,
    *,
    requested_as_of: str | date | None,
    statement_as_of: date,
    align_to_latest_snapshot: bool,
) -> date:
    if requested_as_of:
        parsed = requested_as_of if isinstance(requested_as_of, date) else _parse_ib_date(requested_as_of)
        if not parsed:
            raise IBKRFlexError(f"Invalid as_of override: {requested_as_of}")
        target = parsed
    elif align_to_latest_snapshot:
        current = _latest_portfolio_as_of(conn)
        target = current or statement_as_of
    else:
        target = statement_as_of
    if target != statement_as_of:
        raise IBKRFlexError(
            f"Snapshot date {target} does not match IBKR report date {statement_as_of}; "
            "report values cannot be relabelled as another business date"
        )
    return target


def _ensure_reference_data(conn, account_code: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pams_accounts (account_code, display_name, base_currency, is_asset, active)
            VALUES (%s, %s, 'USD', TRUE, TRUE)
            ON CONFLICT (account_code) DO NOTHING
            """,
            (account_code, "盈透证券"),
        )
        if not _table_exists(conn, "pams_asset_categories"):
            return
        cur.execute(
            """
            INSERT INTO pams_asset_categories (major_category, minor_category)
            VALUES
                ('美股', '美股基金'),
                ('现金', '现金/活期'),
                ('借款', '借款'),
                ('其他', '期权'),
                ('其他', '债券'),
                ('其他', '基金'),
                ('其他', '商品'),
                ('其他', '待分类')
            ON CONFLICT (major_category, minor_category) DO NOTHING
            """
        )


def _write_holdings(conn, rows: list[dict[str, Any]], *, account_code: str, as_of: date,
                    retirement_sources: dict | None = None, expected_report_sha256: str | None = None) -> int:
    if any(row.get("as_of") != as_of for row in rows):
        raise IBKRFlexError("Holdings row date does not match the report snapshot date")
    named = [row['name'] for row in rows if isinstance(row.get('name'), str) and row['name']]
    if len(set(named)) != len(named):
        raise ValueError('holding result set is ambiguous: duplicate names')
    from domain.investments import holding_write_evidence as evidence
    isolated = evidence.enabled(conn)
    evidence.lock_account(conn, account_code)
    if not isolated:
        raise ValueError('unified source synchronization requires isolated mode')
    names = [row.get('name') for row in rows]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError('holding result set is empty or ambiguous')
    if any(row.get('account_code') != account_code for row in rows):
        raise ValueError('holding account conflicts with writer scope')
    retirements, preserved = evidence.verify_ibkr_retirements(conn, account_code=account_code, business_date=as_of,
                                                              incoming_names=names, sources=retirement_sources or {},
                                                              expected_report_sha256=expected_report_sha256)
    if not names and not retirements:
        if preserved:
            return 0
        raise ValueError('holding result set is empty or ambiguous')
    evidence.reject_identity_conflicts(conn, account_code=account_code, business_date=as_of, incoming=rows)
    with conn.cursor() as cur:
        cur.execute('SAVEPOINT ibkr_holding_evidence')
    try:
        evidence.capture_existing_holdings(conn, account_code=account_code, business_date=as_of,
                                          names=names + [item['name'] for item in retirements],
                                          source_by_name={item['name']: item['item_id'] for item in retirements})
        evidence.apply_ibkr_retirements(conn, retirements)
        count = _upsert_holdings(conn, rows, account_code=account_code, as_of=as_of)
        if names:
            evidence.capture_written_holdings(conn, account_code=account_code, business_date=as_of,
                                              names=names, origin='ibkr_flex')
        evidence.finish_ibkr_retirements(conn, retirements)
        with conn.cursor() as cur:
            cur.execute('RELEASE SAVEPOINT ibkr_holding_evidence')
        return count
    except Exception:
        with conn.cursor() as cur:
            cur.execute('ROLLBACK TO SAVEPOINT ibkr_holding_evidence')
            cur.execute('RELEASE SAVEPOINT ibkr_holding_evidence')
        raise


def _upsert_holdings(conn, rows: list[dict[str, Any]], *, account_code: str, as_of: date) -> int:
    if any(row.get("as_of") != as_of for row in rows):
        raise IBKRFlexError("Holdings row date does not match the report snapshot date")
    rows = [dict(row) for row in rows]
    _ensure_reference_data(conn, account_code)
    can_backfill_asset_id = (
        _column_exists(conn, "pams_holdings_snapshot", "asset_id")
        and _table_exists(conn, "pams_asset_master")
    )
    with conn.cursor() as cur:
        unchanged_guard = """WHERE (pams_holdings_snapshot.symbol,pams_holdings_snapshot.asset_liability,
            pams_holdings_snapshot.currency,pams_holdings_snapshot.major_category,pams_holdings_snapshot.minor_category,
            pams_holdings_snapshot.market_value,pams_holdings_snapshot.cost,pams_holdings_snapshot.quantity,
            pams_holdings_snapshot.buy_price,pams_holdings_snapshot.current_price)
            IS DISTINCT FROM (EXCLUDED.symbol,EXCLUDED.asset_liability,EXCLUDED.currency,EXCLUDED.major_category,
            EXCLUDED.minor_category,EXCLUDED.market_value,EXCLUDED.cost,EXCLUDED.quantity,EXCLUDED.buy_price,EXCLUDED.current_price)"""
        psycopg2.extras.execute_batch(
            cur,
            f"""
            INSERT INTO pams_holdings_snapshot (
                symbol, name, asset_liability, currency, account_code,
                major_category, minor_category, market_value, cost,
                quantity, buy_price, current_price, as_of
            ) VALUES (
                %(symbol)s, %(name)s, %(asset_liability)s, %(currency)s, %(account_code)s,
                %(major_category)s, %(minor_category)s, %(market_value)s, %(cost)s,
                %(quantity)s, %(buy_price)s, %(current_price)s, %(as_of)s
            )
            ON CONFLICT (account_code, name, as_of)
            DO UPDATE SET
                symbol = EXCLUDED.symbol,
                asset_liability = EXCLUDED.asset_liability,
                currency = EXCLUDED.currency,
                major_category = EXCLUDED.major_category,
                minor_category = EXCLUDED.minor_category,
                market_value = EXCLUDED.market_value,
                cost = EXCLUDED.cost,
                quantity = EXCLUDED.quantity,
                buy_price = EXCLUDED.buy_price,
                current_price = EXCLUDED.current_price,
                updated_at = NOW()
            {unchanged_guard}
            """,
            rows,
            page_size=200,
        )
        if can_backfill_asset_id:
            # A name or symbol match is usable only when every identity candidate
            # resolves to the same asset. Priority ordering cannot prove identity.
            alias_candidates = ""
            if _table_exists(conn, "pams_asset_alias"):
                alias_candidates = f"""
                    UNION
                    SELECT h.id, aa.asset_id FROM targets h
                    JOIN pams_asset_alias aa
                      ON LOWER(aa.alias_value) = LOWER(COALESCE(NULLIF(h.symbol, ''), h.name))
                     AND {identity_alias_sql('aa')}
                    JOIN pams_asset_master am ON am.asset_id=aa.asset_id AND am.status='active'
                """
            cur.execute(
                f"""
                WITH targets AS (
                    SELECT id,symbol,name FROM pams_holdings_snapshot
                    WHERE account_code=%s AND as_of=%s AND asset_id IS NULL
                ), candidates AS (
                    SELECT h.id,am.asset_id FROM targets h
                    JOIN pams_asset_master am ON am.primary_symbol=h.symbol AND am.status='active'
                    {alias_candidates}
                ), unique_match AS (
                    SELECT id, MIN(asset_id) AS asset_id FROM candidates
                    GROUP BY id HAVING COUNT(DISTINCT asset_id)=1
                )
                UPDATE pams_holdings_snapshot h SET asset_id=m.asset_id,updated_at=NOW()
                FROM unique_match m WHERE h.id=m.id
                """,
                (account_code,as_of),
            )
    return len(rows)


def _validate_isolated_holding_inputs(parsed, observed, account_code, xml_text):
    """Do not turn missing or unrepresentable report fields into holdings."""
    from decimal import Decimal, InvalidOperation
    from domain.imports.ibkr_holding_source import _extract, _number
    if account_code!='ibkr' or len(parsed.source_account_ids)!=1:
        raise ValueError('isolated holdings need one report account')
    _,_,positions=_extract(xml_text.encode('utf8'),source_account_id=parsed.source_account_ids[0],
                            business_date=observed,max_bytes=32*1024*1024)
    identities=set()
    # Match the original symbol and currency directly; classification is independent.
    live=parsed.rows
    for position in positions:
        if set(position['unknown_fields'])-{'cost'}:
            raise ValueError('IBKR holding source fields are incomplete')
        attrs=position['raw_attributes']; identity=(position['symbol'].upper(),position['currency'])
        if identity in identities:
            raise ValueError('IBKR holding source identity is ambiguous')
        identities.add(identity)
        quantity=_number(attrs,('position','quantity','qty'))
        value=_number(attrs,('marketValue','positionValue','value','markValue'))
        cost=_number(attrs,('costBasisMoney','costBasis','cost'))
        try:
            if (quantity is None or value is None or quantity!=quantity.quantize(Decimal('0.000001'))
                    or value!=value.quantize(Decimal('0.0001'))
                    or (cost is not None and cost!=cost.quantize(Decimal('0.0001')))):
                raise ValueError('IBKR holding source precision is unrepresentable')
        except InvalidOperation as exc:
            raise ValueError('IBKR holding source precision is unrepresentable') from exc
        if quantity==0 and value==0:
            continue
        matches=[row for row in live if (row.get('symbol'),row.get('currency'))==identity]
        if len(matches)!=1:
            raise ValueError('IBKR holding parser and source do not match')
        actual=matches[0]
        for key,expected in (('quantity',quantity),('market_value',value),('cost',cost)):
            if (expected is None and actual.get(key) is not None) or (expected is not None and
                    (actual.get(key) is None or Decimal(str(actual[key]))!=expected)):
                raise ValueError('IBKR holding parser changed source precision')
        multiplier=_number(attrs,('multiplier',))
        if 'multiplier' in attrs and (multiplier is None or multiplier<=0):
            raise ValueError('IBKR holding multiplier is invalid')
        for target,keys in (('current_price',('markPrice','closePrice','price')),
                            ('buy_price',('costBasisPrice','avgCost','averageCost'))):
            supplied=any(str(attrs.get(key) or '').strip() for key in keys)
            price=_number(attrs,keys)
            if supplied:
                try:
                    if (price is None or price!=price.quantize(Decimal('0.000001')) or
                            actual.get(target) is None or Decimal(str(actual[target]))!=price):
                        raise ValueError('IBKR holding price evidence is inconsistent')
                except InvalidOperation as exc:
                    raise ValueError('IBKR holding price evidence is inconsistent') from exc
            elif multiplier is None:
                # A contract multiplier cannot be inferred from symbol or asset type.
                actual[target]=None
        actual['ibkr_conid']=str(attrs.get('conid') or '').strip() or None
        actual['ibkr_report_account']=parsed.source_account_ids[0]

    # A source row, including zero, is never an absent or guessed cash balance.
    from domain.brokers.ibkr.ibkr_daily_close import _document
    from domain.brokers.ibkr.ibkr_cash_event_sync import _cash_scope_ok
    evidence,_,error=_document(xml_text,parsed.source_account_ids[0],None)
    if error or evidence is None:
        raise ValueError('IBKR cash report is unsafe or invalid')
    statements=[s for s in evidence.statements if _parse_ib_date(s.get('toDate'))==observed]
    if len(statements)!=1:
        raise ValueError('IBKR cash statement date is ambiguous')
    seen_cash=set()
    nodes=[n for n in statements[0].iter() if _local_name(n.tag).lower()=='cashreportcurrency']
    for node in nodes:
        attrs=dict(node.attrib); currency=attrs.get('currency')
        if currency=='BASE_SUMMARY':
            continue
        if (not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency) or currency in seen_cash
                or not _cash_scope_ok(node,evidence.parents,parsed.source_account_ids[0])
                or _parse_ib_date(attrs.get('toDate'))!=observed):
            raise ValueError('IBKR cash currency or scope is missing or ambiguous')
        seen_cash.add(currency)
        amount=_number(attrs,('endingCash',))
        try:
            if amount is None or amount!=amount.quantize(Decimal('0.0001')):
                raise ValueError('IBKR cash amount is missing or unrepresentable')
        except InvalidOperation as exc:
            raise ValueError('IBKR cash amount is invalid') from exc
        matches=[row for row in parsed.rows if row.get('symbol')=='CASH_'+currency and row.get('currency')==currency]
        if (len(matches)!=1 or matches[0].get('market_value') is None or
                Decimal(str(matches[0]['market_value']))!=amount):
            raise ValueError('IBKR cash parser and source do not match')
    parsed_cash={row.get('currency') for row in parsed.rows if row.get('symbol')=='CASH_'+str(row.get('currency') or '')}
    if parsed_cash!=seen_cash:
        raise ValueError('IBKR cash source is incomplete')


def _validated_isolated_report_trades(parsed, observed, account_code, xml_text, report_sha256):
    """Select explicit current-day rows from the verified report only."""
    if account_code != 'ibkr' or len(parsed.source_account_ids) != 1:
        raise ValueError('isolated report needs one verified IBKR account')
    official_account = parsed.source_account_ids[0]
    from domain.brokers.ibkr.ibkr_daily_close import _document
    from domain.brokers.ibkr.ibkr_cash_event_sync import _cash_scope_ok
    report, _, error = _document(xml_text, official_account, report_sha256)
    if error:
        raise ValueError('current report trade source is unsafe or invalid')
    statements = [statement for statement in report.statements if _parse_ib_date(statement.get('toDate')) == observed]
    if len(statements) != 1:
        raise ValueError('current report statement is ambiguous')
    nodes = [node for node in statements[0].iter() if node.tag.rsplit('}', 1)[-1] == 'Trade']
    if any(trade['trade_date'] > observed for trade in parsed.trades):
        raise ValueError('current report contains a future trade')
    current = [trade for trade in parsed.trades if trade['trade_date'] == observed]
    if any(trade.get('source_account_id') != official_account for trade in current):
        raise ValueError('current report trade account is missing or inconsistent')
    for trade in current:
        raw = trade.get('raw') or {}
        if _parse_ib_date(raw.get('tradeDate')) != observed:
            raise ValueError('current report trade lacks an explicit business date')
        matches = [node for node in nodes if {str(k): str(v).strip() for k, v in node.attrib.items()} == raw]
        if len(matches) != 1 or not _cash_scope_ok(matches[0], report.parents, official_account):
            raise ValueError('current report trade source scope is ambiguous')
        identifier = str(raw.get('tradeID') or raw.get('transactionID') or raw.get('ibExecID') or '').strip()
        if not identifier or identifier.lower() in {'0', 'none', 'null', 'unknown'}:
            raise ValueError('current report trade lacks official identity')
    keys = [trade['source_trade_key'] for trade in current]
    if len(keys) != len(set(keys)):
        raise ValueError('current report trade identity is ambiguous')
    return official_account, current, {'older_in_report_untouched': len(parsed.trades) - len(current)}


def _verify_isolated_report_trade_replay(conn, parsed, observed, account_code, xml_text, report_sha256,
                                         cash_entries=None, cash_pending_count=0, trade_cutoff=None):
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
    official, trades, stats = _validated_isolated_report_trades(parsed, observed, account_code, xml_text, report_sha256)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        result = publish_report_batch(cur, report_bytes=xml_text.encode('utf8'), source_account_id=official,
                                      day=observed, report_sha256=report_sha256, replay=True,
                                      cash_entries=cash_entries, cash_pending_count=cash_pending_count, trade_cutoff=trade_cutoff)
    return {**result, 'replayed_verified': result['existing_verified'], 'historical_backfill': 'not_run',
            'historical_positions': 'not_run', 'legacy_funding_confirmation': 'not_run', **stats}


def record_sync_run(
    conn, *, source: str = SYNC_SOURCE, status: str, started_at: datetime,
    as_of: date | None = None, statement_as_of: date | None = None,
    row_count: int = 0, trade_count: int = 0, history_row_count: int = 0,
    raw_file: str | None = None, message: str | None = None,
    confirmed_funding_count: int = 0, error: str | None = None,
    publication: dict[str, Any] | None = None, exception: BaseException | None = None,
) -> dict[str, Any]:
    """Commit business publication only; emit technical outcome to structured logging."""
    summary = {"statement_as_of": statement_as_of.isoformat() if statement_as_of else None,
        "row_count":row_count,"trade_count":trade_count,"history_row_count":history_row_count,
        "confirmed_funding_count":confirmed_funding_count,"raw_file":raw_file,"message":message}
    if publication is not None:
        summary['publication'] = publication
    identifier = None
    if status == 'success' and publication and publication.get('state') in {'published','unchanged'}:
        source_batch_id = (publication.get('report_trade_sources') or {}).get('batch_id')
        if not source_batch_id or not statement_as_of or not publication.get('sha256') or not publication.get('data_sha256'):
            raise ValueError('published report identity is incomplete')
        cash_state = None
        if isinstance(publication.get('cash_events'), dict):
            from domain.brokers.ibkr.ibkr_cash_event_sync import _pending_business_entries
            pending = _pending_business_entries(publication['cash_events'].get('pending') or [])
            cash_state = {'pending':pending, 'cutoff':publication['cash_events'].get('cutoff'),
                'inserted_count':None,'duplicate_count':None,'historical_count':None,
                'retryable_count':sum(1 for item in pending if item.get('retryable')),
                'blocking_pending_count':sum(1 for item in pending if item.get('blocking')),
                'bank_match_pending_count':sum(1 for item in pending if item.get('reason')=='bank_match_pending')}
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # The same lock serializes both first publication and state-only replay.
            cur.execute("SELECT pg_advisory_xact_lock(7362198401)")
            cur.execute("SELECT published_result::text AS result_text FROM source_batches WHERE batch_id=%s AND status='published'", (source_batch_id,))
            source_result = cur.fetchone()
            if not source_result or source_result['result_text'] is None:
                raise ValueError('published source result is unavailable')
            result_hash = hashlib.sha256(source_result['result_text'].encode('utf8')).hexdigest()
            cur.execute("""SELECT * FROM broker_processing_state
                WHERE state_type='publication' AND source_batch_id=%s AND replay_fingerprint=%s ORDER BY publication_id DESC LIMIT 1""",
                (source_batch_id,publication['data_sha256']))
            previous = cur.fetchone()
            if previous:
                if (previous['account_code']!=publication['account_code'] or previous['statement_date']!=statement_as_of
                        or str(previous['source_sha256']).strip()!=publication['sha256']
                        or str(previous['published_result_sha256']).strip()!=result_hash):
                    raise ValueError('conflicting report publication identity')
                row_count = previous['holding_count']
                if cash_state is None:
                    cash_state = previous['cash_state']
            cash_hash = hashlib.sha256(json.dumps(cash_state,sort_keys=True,separators=(',', ':'),
                ensure_ascii=False,allow_nan=False).encode('utf8')).hexdigest()
            if previous and cash_state == previous['cash_state']:
                if cash_hash != str(previous['cash_state_sha256']).strip():
                    raise ValueError('previous cash publication state is inconsistent')
                identifier = previous['publication_id']
            else:
                cur.execute("""INSERT INTO broker_processing_state
                    (state_type,account_code,source_batch_id,statement_date,source_sha256,replay_fingerprint,published_result_sha256,
                     holding_count,cash_state,cash_state_sha256)
                    VALUES ('publication',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING publication_id""",
                    (publication['account_code'],source_batch_id,statement_as_of,publication['sha256'],
                     publication['data_sha256'],result_hash,row_count,
                     psycopg2.extras.Json(cash_state) if cash_state is not None else None,cash_hash))
                identifier = cur.fetchone()['publication_id']
    from domain.operations.runtime_events import emit_sync_attempt_result, emit_sync_commit_failed, emit_sync_run_committed
    try:
        conn.commit()
    except Exception as exc:
        emit_sync_commit_failed(exception=exc)
        raise
    if status == 'success':
        emit_sync_run_committed(status)
    else:
        emit_sync_attempt_result(status, exception=exception)
    return _sync_row_to_status({'id':identifier,'sync_key':source,'asset_scope':'ibkr','status':status,
        'started_at':started_at,'finished_at':None,'target_trade_date':as_of,'rows_upserted':summary['row_count'],
        'summary_json':summary,'error_text':error})


def latest_sync_status(conn, *, source: str = SYNC_SOURCE) -> dict[str, Any] | None:
    """Return the latest business publication; the last technical attempt is unknown."""
    if source != SYNC_SOURCE:
        return None
    records = report_state._records(conn)
    if not records:
        return None
    report, _ = report_state.select_report(conn, default_data_dir() / 'raw' / 'ibkr')
    if report is None:
        return {'source':source,'status':'unknown','publication':None,'cash_events':None,
                'message':'published report evidence unavailable'}
    selected = next((row for row in records if row['id']==report.run_id), None)
    if selected is None:
        return {'source':source,'status':'unknown','publication':None,'cash_events':None,
                'message':'published report identity unavailable'}
    result = _sync_row_to_status(selected)
    state = (selected.get('summary_json') or {}).get('publication') or {}
    if not state.get('cash_state_valid', False) or state.get('cash_events') is None:
        result['cash_events'] = None
        result['cash_evidence_status'] = 'unknown'
    else:
        result['cash_evidence_status'] = 'verified_publication_state'
    result.update(source=source,asset_scope='ibkr',status='unknown',started_at=None,finished_at=None,error=None,
                  message='latest technical attempt unavailable from business publication')
    return result


def _sync_row_to_status(row: dict[str, Any]) -> dict[str, Any]:
    summary = row.get("summary_json") or {}
    if not isinstance(summary, dict):
        summary = {}
    status = {
        "id": row.get("id"),
        "source": row.get("sync_key"),
        "asset_scope": row.get("asset_scope"),
        "status": row.get("status"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "as_of": row.get("target_trade_date"),
        "statement_as_of": summary.get("statement_as_of"),
        "row_count": row.get("rows_upserted") if row.get("rows_upserted") is not None else summary.get("row_count"),
        "trade_count": summary.get("trade_count"),
        "history_row_count": summary.get("history_row_count"),
        "confirmed_funding_count": summary.get("confirmed_funding_count"),
        "raw_file": summary.get("raw_file"),
        "message": summary.get("message"),
        "error": row.get("error_text"),
        **({"publication": summary["publication"]} if "publication" in summary else {}),
    }
    publication = summary.get("publication")
    if isinstance(publication, dict) and isinstance(publication.get("cash_events"), dict):
        status["cash_events"] = publication["cash_events"]
    return status


def sync_ibkr_flex_to_db(
    conn,
    *,
    token: str,
    query_id: str,
    account_code: str = "ibkr",
    requested_as_of: str | date | None = None,
    align_to_latest_snapshot: bool = False,
    raw_dir: Path | None = None,
    flex_version: int = 3,
    after_write: Callable[[Any], None] | None = None,
    expected_statement_reference: date | None = None,
    holding_retirement_sources: dict | None = None,
) -> dict[str, Any]:
    """Sync a report atomically, including optional dependent writes.

    `after_write` shares this transaction and must not commit or roll back.
    In the isolated source path the original XML is stored transactionally.
    """
    ensure_sync_table(conn)
    if holding_retirement_sources is None:
        holding_retirement_sources = {}
    from domain.investments import holding_write_evidence as evidence
    if not evidence.enabled(conn):
        raise ValueError('unified source synchronization requires isolated mode')
    for table in ('source_batches', 'source_items', 'pams_investment_trade_fact', 'pams_fx_rates_daily'):
        _require_schema_table(conn, table)
    started_at = datetime.now(timezone.utc)
    raw_reference: str | None = None
    publication: dict[str, Any] = {"version": 1, "state": "rejected", "account_code": account_code}
    observed: date | None = None
    try:
        cash_events_after = ibkr_cash_events.configured_cutoff()
        reference = request_flex_reference(token, query_id, flex_version=flex_version)
        xml_text = fetch_flex_statement(
            token,
            str(reference["reference_code"]),
            flex_version=flex_version,
        )
        publication['sha256'] = hashlib.sha256(xml_text.encode('utf8')).hexdigest()
        observed = report_state.inspect_report(xml_text)
        parsed = parse_flex_report(xml_text, account_code=account_code)
        if observed != parsed.statement_as_of:
            raise report_state.ReportRejected("parsed_statement_date_mismatch")
        cash_events = [{**event, "source_evidence": {"broker_source": SYNC_SOURCE,
            "source_sha256": publication["sha256"], "confirmed_day": observed.isoformat()}}
            for event in parsed.cash_events]
        publication["report_date"] = observed.isoformat()
        expected = report_state.expected_statement_date(expected_statement_reference)
        publication["expected_date"] = expected.isoformat()
        # Serialize publishers before selecting the active report or touching business rows.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7362198401)")
        previous, _ = report_state.select_report(conn, raw_dir or default_data_dir() / 'raw' / 'ibkr')
        report_state.validate_candidate(observed, expected, previous, minimum=report_state.publication_floor(conn))
        # Exact XML identity includes performance-only fields not present in parsed holdings.
        # A standalone import must not suppress a later import with dependent fact writes.
        data_identity = f"{publication['sha256']}:{after_write is not None}"
        publication["data_sha256"] = hashlib.sha256(data_identity.encode("ascii")).hexdigest()
        has_position_rows = any(row.get("major_category") == "美股" for row in parsed.rows)
        if not has_position_rows and _contains_tag(xml_text, "PriorPeriodPosition"):
            raise IBKRFlexError(
                "IBKR Flex query includes Prior Period Positions but no Open Positions. "
                "Edit the query and add 'Open Positions' / '未平仓仓位' before syncing."
        )
        target_date = _target_as_of(
            conn,
            requested_as_of=requested_as_of,
            statement_as_of=parsed.statement_as_of,
            align_to_latest_snapshot=align_to_latest_snapshot,
        )
        if holding_retirement_sources is not None and target_date != observed:
            raise ValueError('retirement source requires the current report date')
        if holding_retirement_sources and cash_events_after is not None and target_date <= cash_events_after:
            raise ValueError('retirement source is outside the active report cutoff')
        if holding_retirement_sources is not None:
            _validate_isolated_holding_inputs(parsed,observed,account_code,xml_text)
        if target_date == observed and previous and previous.data_sha256 == publication["data_sha256"]:
            replay_cash_entries=[]
            if holding_retirement_sources is not None:
                _write_holdings(conn, parsed.rows, account_code=account_code, as_of=target_date,
                                retirement_sources=holding_retirement_sources,
                                expected_report_sha256=publication['sha256'])
                if cash_events_after is not None:
                    publication["cash_events"] = ibkr_cash_events.import_cash_events(
                        conn, cash_events, account_code=account_code, cutoff=cash_events_after,
                        statement_as_of=observed, defer_source_link=True)
                    replay_cash_entries=publication["cash_events"].pop('_deferred_source_events',[])
                publication['report_trade_sources'] = _verify_isolated_report_trade_replay(
                    conn, parsed, observed, account_code, xml_text, publication['sha256'],
                    cash_entries=replay_cash_entries,
                    cash_pending_count=publication.get('cash_events',{}).get('blocking_pending_count',0),
                    trade_cutoff=cash_events_after)
                replay_batch=publication['report_trade_sources'].get('batch_id')
                if replay_batch:
                    raw_reference='source:'+replay_batch
                elif previous and isinstance(previous.path,Path):
                    raw_reference=str(previous.path)
                else:
                    raise ValueError('replayed IBKR report source is unavailable')
            publication["state"] = "unchanged"
            record = record_sync_run(
                conn, status="success", started_at=started_at, as_of=target_date,
                statement_as_of=observed, raw_file=raw_reference, publication=publication,
                message="Identical report; active publication retained" if cash_events_after is not None
                else "Identical report; active publication and business rows retained",
            )
            return {
                "success": True, "status": "success", "as_of": target_date.isoformat(),
                **({"source_mode": "unified"} if holding_retirement_sources is not None else {}),
                "statement_as_of": observed.isoformat(), "row_count": 0, "trade_count": 0,
                "history_row_count": 0, "confirmed_funding_count": 0, "confirmed_fundings": [],
                "raw_file": raw_reference, "reference_code": reference["reference_code"],
                "sync_run": record, "expected_statement_as_of": expected.isoformat(),
            }
        from domain.investments.holding_write_evidence import lock_account
        lock_account(conn, account_code)
        isolated_account, isolated_trades, isolated_trade_stats = _validated_isolated_report_trades(
            parsed, observed, account_code, xml_text, publication['sha256'])
        row_count = _write_holdings(conn, parsed.rows, account_code=account_code, as_of=target_date,
                                    retirement_sources=holding_retirement_sources,
                                    expected_report_sha256=publication['sha256'])
        history_row_count = 0
        confirmed_fundings = []
        deferred_cash = []
        if cash_events_after is not None:
            publication['cash_events'] = ibkr_cash_events.import_cash_events(
                conn, cash_events, account_code=account_code, cutoff=cash_events_after,
                statement_as_of=observed, defer_source_link=True)
            deferred_cash = publication['cash_events'].pop('_deferred_source_events', [])
        from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
        selected = isolated_trades if cash_events_after is None else [
            row for row in isolated_trades if row['trade_date'] > cash_events_after]
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if selected and len(selected) != len(isolated_trades):
                raise ValueError('partial IBKR report trade selection is not a complete source batch')
            trade_result = publish_report_batch(cur, report_bytes=xml_text.encode('utf8'), source_account_id=isolated_account,
                day=observed, report_sha256=publication['sha256'], cash_entries=deferred_cash,
                cash_pending_count=publication.get('cash_events', {}).get('blocking_pending_count', 0),
                trade_cutoff=cash_events_after)
        trade_count = trade_result['written']
        publication['report_trade_sources'] = {**trade_result, 'selected': len(selected),
            'historical_backfill': 'not_run', 'historical_positions': 'not_run',
            'legacy_funding_confirmation': 'not_run', **isolated_trade_stats}
        raw_reference = 'source:' + trade_result['batch_id']
        if after_write is not None:
            after_write(conn)
        publication["state"] = "published" if target_date == observed else "historical"
        record = record_sync_run(
            conn,
            status="success",
            started_at=started_at,
            as_of=target_date,
            statement_as_of=parsed.statement_as_of,
            row_count=row_count,
            trade_count=trade_count,
            history_row_count=history_row_count,
            raw_file=raw_reference,
            message=(
                f"reference={reference['reference_code']}; "
                f"accounts={','.join(parsed.source_account_ids)}; "
                f"trades={trade_count}; history_rows={history_row_count}; "
                f"confirmed_fundings={len(confirmed_fundings)}"
            ),
            confirmed_funding_count=len(confirmed_fundings),
            publication=publication,
        )
        return {
            **({"source_mode": "unified"} if holding_retirement_sources is not None else {}),
            "success": True,
            "status": "success",
            "as_of": target_date.isoformat(),
            "statement_as_of": parsed.statement_as_of.isoformat(),
            "row_count": row_count,
            "trade_count": trade_count,
            "history_row_count": history_row_count,
            "confirmed_funding_count": len(confirmed_fundings),
            "confirmed_fundings": [
                {
                    **item,
                    "event_date": item.get("event_date").isoformat()
                    if hasattr(item.get("event_date"), "isoformat")
                    else item.get("event_date"),
                    "movement": {
                        **(item.get("movement") or {}),
                        "movement_date": (item.get("movement") or {}).get("movement_date").isoformat()
                        if hasattr((item.get("movement") or {}).get("movement_date"), "isoformat")
                        else (item.get("movement") or {}).get("movement_date"),
                    },
                }
                for item in confirmed_fundings
            ],
            "raw_file": raw_reference,
            "reference_code": reference["reference_code"],
            "sync_run": record,
            "expected_statement_as_of": expected.isoformat(),
        }
    except report_state.ReportRejected as exc:
        conn.rollback()
        record = record_sync_run(
            conn, status="blocked", started_at=started_at, statement_as_of=observed,
            raw_file=raw_reference, publication=publication, error=str(exc), exception=exc,
            message="Candidate rejected before business writes; previous publication retained",
        )
        return {
            "success": False, "status": "blocked", "reason": str(exc),
            "statement_as_of": observed.isoformat() if observed else None,
            "expected_statement_as_of": publication.get("expected_date"), "sync_run": record,
        }
    except Exception as exc:
        try:
            conn.rollback()
            publication["state"] = "rejected"
            record_sync_run(
                conn,
                status="failed",
                started_at=started_at,
                raw_file=raw_reference,
                error=str(exc), exception=exc,
                publication=publication,
            )
        except Exception as audit_exc:
            from domain.operations.runtime_events import emit_sync_commit_failed
            emit_sync_commit_failed(exception=audit_exc)
        raise
