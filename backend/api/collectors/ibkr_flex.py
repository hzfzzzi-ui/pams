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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2.extras
from psycopg2.extras import Json
import requests

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
    return {
        "source_account_id": _attr(attrs, "accountId", "acctId", default=None),
        "symbol": symbol,
        "name": name,
        "asset_liability": "资产" if market_value >= 0 else "负债",
        "currency": currency,
        "account_code": account_code,
        "major_category": "美股",
        "minor_category": "美股基金",
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
    if cash is None or abs(cash) < 1e-8:
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


def parse_flex_report(xml_text: str, *, account_code: str = "ibkr") -> FlexParseResult:
    """Parse IBKR Flex XML into PAMS holdings rows."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise IBKRFlexError(f"Invalid IBKR Flex report XML: {exc}") from exc

    source_accounts: set[str] = set()
    history_rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []

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
            elif local == "trade":
                trade = _trade_row(elem, account_code, fallback_as_of)
                if trade:
                    trades.append(trade)
                    if trade.get("source_account_id"):
                        source_accounts.add(str(trade["source_account_id"]))

    if not history_rows:
        raise IBKRFlexError("No holdings or cash rows were parsed from the IBKR Flex report")

    row_dates = [r["as_of"] for r in history_rows if isinstance(r.get("as_of"), date)]
    statement_as_of = max(row_dates)
    rows = [row for row in history_rows if row.get("as_of") == statement_as_of]
    if not rows:
        raise IBKRFlexError(f"No current rows were parsed for latest IBKR statement date {statement_as_of}")

    return FlexParseResult(
        rows=rows,
        statement_as_of=statement_as_of,
        source_account_ids=sorted(source_accounts),
        trades=trades,
        history_rows=history_rows,
    )


def default_data_dir() -> Path:
    """Return the persistent local data directory used by the API container."""
    return Path(os.environ.get("PAMS_DATA_DIR", "/app/data"))


def save_raw_flex_xml(xml_text: str, query_id: str, *, raw_dir: Path | None = None) -> Path:
    """Persist a downloaded Flex XML report under `data/raw/ibkr`."""
    directory = raw_dir or (default_data_dir() / "raw" / "ibkr")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_query = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(query_id))[:80]
    path = directory / f"{stamp}_query_{safe_query}.xml"
    path.write_text(xml_text, encoding="utf-8")
    return path


def ensure_sync_table(conn) -> None:
    """Create the generic sync run table used by collector status endpoints."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pams_data_sync_runs (
                id BIGSERIAL PRIMARY KEY,
                sync_key TEXT NOT NULL,
                asset_scope TEXT NOT NULL,
                target_trade_date DATE,
                status TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                latest_trade_date_before DATE,
                latest_trade_date_after DATE,
                rows_upserted INTEGER,
                summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                error_text TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pams_data_sync_runs_sync_key_started_at
                ON pams_data_sync_runs (sync_key, started_at DESC)
            """
        )


def ensure_ibkr_trade_table(conn) -> None:
    """Create the local read-only IBKR trade ledger used for reconciliation."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pams_ibkr_trades (
                id BIGSERIAL PRIMARY KEY,
                account_code VARCHAR(50) NOT NULL REFERENCES pams_accounts(account_code),
                source_account_id TEXT,
                source_trade_key TEXT NOT NULL,
                trade_id TEXT,
                transaction_id TEXT,
                order_id TEXT,
                symbol VARCHAR(100) NOT NULL,
                name VARCHAR(200) NOT NULL,
                currency VARCHAR(10) NOT NULL,
                asset_category TEXT,
                trade_date DATE NOT NULL,
                side VARCHAR(12),
                quantity NUMERIC(28, 10),
                price NUMERIC(28, 10),
                gross_amount NUMERIC(28, 10),
                commission NUMERIC(28, 10),
                net_cash NUMERIC(28, 10),
                realized_pnl NUMERIC(28, 10),
                raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (account_code, source_trade_key)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pams_ibkr_trades_trade_date
                ON pams_ibkr_trades (trade_date DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pams_ibkr_trades_symbol_date
                ON pams_ibkr_trades (symbol, trade_date DESC)
            """
        )


def ensure_ibkr_position_history_table(conn) -> None:
    """Create the local IBKR historical position ledger."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pams_ibkr_position_history (
                id BIGSERIAL PRIMARY KEY,
                account_code VARCHAR(50) NOT NULL REFERENCES pams_accounts(account_code),
                source_account_id TEXT,
                symbol VARCHAR(100),
                name VARCHAR(200) NOT NULL,
                asset_liability VARCHAR(10) NOT NULL,
                currency VARCHAR(10) NOT NULL,
                major_category VARCHAR(50) NOT NULL,
                minor_category VARCHAR(50) NOT NULL,
                market_value NUMERIC(28, 10) NOT NULL,
                cost NUMERIC(28, 10),
                holding_pnl NUMERIC(28, 10)
                    GENERATED ALWAYS AS (market_value - COALESCE(cost, 0)) STORED,
                quantity NUMERIC(28, 10),
                buy_price NUMERIC(28, 10),
                current_price NUMERIC(28, 10),
                as_of DATE NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (account_code, name, as_of)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pams_ibkr_position_history_as_of
                ON pams_ibkr_position_history (as_of DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pams_ibkr_position_history_symbol_asof
                ON pams_ibkr_position_history (symbol, as_of DESC)
            """
        )


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
        return parsed
    if align_to_latest_snapshot:
        current = _latest_portfolio_as_of(conn)
        if current:
            return current
    return statement_as_of


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
                ('借款', '借款')
            ON CONFLICT (major_category, minor_category) DO NOTHING
            """
        )


def _write_holdings(conn, rows: list[dict[str, Any]], *, account_code: str, as_of: date) -> int:
    _ensure_reference_data(conn, account_code)
    can_backfill_asset_id = (
        _column_exists(conn, "pams_holdings_snapshot", "asset_id")
        and _table_exists(conn, "pams_asset_master")
    )
    for row in rows:
        row["as_of"] = as_of
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pams_holdings_snapshot WHERE account_code = %s AND as_of = %s",
            (account_code, as_of),
        )
        psycopg2.extras.execute_batch(
            cur,
            """
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
            """,
            rows,
            page_size=200,
        )
        if can_backfill_asset_id:
            cur.execute(
                """
                WITH primary_match AS (
                    SELECT DISTINCT ON (h.id) h.id, am.asset_id
                    FROM pams_holdings_snapshot h
                    JOIN pams_asset_master am
                      ON am.primary_symbol = h.symbol
                     AND am.status = 'active'
                    WHERE h.account_code = %s
                      AND h.as_of = %s
                      AND h.asset_id IS NULL
                      AND h.symbol IS NOT NULL
                    ORDER BY h.id, am.asset_id
                )
                UPDATE pams_holdings_snapshot h
                SET asset_id = pm.asset_id,
                    updated_at = NOW()
                FROM primary_match pm
                WHERE h.id = pm.id
                """,
                (account_code, as_of),
            )
            if _table_exists(conn, "pams_asset_alias"):
                cur.execute(
                    """
                    WITH alias_match AS (
                        SELECT DISTINCT ON (h.id) h.id, aa.asset_id
                        FROM pams_holdings_snapshot h
                        JOIN pams_asset_alias aa
                          ON LOWER(aa.alias_value) = LOWER(COALESCE(NULLIF(h.symbol, ''), h.name))
                        JOIN pams_asset_master am
                          ON am.asset_id = aa.asset_id
                         AND am.status = 'active'
                        WHERE h.account_code = %s
                          AND h.as_of = %s
                          AND h.asset_id IS NULL
                        ORDER BY h.id, aa.priority ASC, aa.asset_id ASC
                    )
                    UPDATE pams_holdings_snapshot h
                    SET asset_id = am.asset_id,
                        updated_at = NOW()
                    FROM alias_match am
                    WHERE h.id = am.id
                    """,
                    (account_code, as_of),
                )
    return len(rows)


def _write_trades(conn, trades: list[dict[str, Any]], *, account_code: str) -> int:
    _ensure_reference_data(conn, account_code)
    ensure_ibkr_trade_table(conn)
    if not trades:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            INSERT INTO pams_ibkr_trades (
                account_code, source_account_id, source_trade_key, trade_id,
                transaction_id, order_id, symbol, name, currency, asset_category,
                trade_date, side, quantity, price, gross_amount, commission,
                net_cash, realized_pnl, raw_json
            ) VALUES (
                %(account_code)s, %(source_account_id)s, %(source_trade_key)s, %(trade_id)s,
                %(transaction_id)s, %(order_id)s, %(symbol)s, %(name)s, %(currency)s, %(asset_category)s,
                %(trade_date)s, %(side)s, %(quantity)s, %(price)s, %(gross_amount)s, %(commission)s,
                %(net_cash)s, %(realized_pnl)s, %(raw)s
            )
            ON CONFLICT (account_code, source_trade_key)
            DO UPDATE SET
                source_account_id = EXCLUDED.source_account_id,
                trade_id = EXCLUDED.trade_id,
                transaction_id = EXCLUDED.transaction_id,
                order_id = EXCLUDED.order_id,
                symbol = EXCLUDED.symbol,
                name = EXCLUDED.name,
                currency = EXCLUDED.currency,
                asset_category = EXCLUDED.asset_category,
                trade_date = EXCLUDED.trade_date,
                side = EXCLUDED.side,
                quantity = EXCLUDED.quantity,
                price = EXCLUDED.price,
                gross_amount = EXCLUDED.gross_amount,
                commission = EXCLUDED.commission,
                net_cash = EXCLUDED.net_cash,
                realized_pnl = EXCLUDED.realized_pnl,
                raw_json = EXCLUDED.raw_json,
                updated_at = NOW()
            """,
            [{**trade, "raw": Json(trade.get("raw") or {})} for trade in trades],
            page_size=200,
        )
    return len(trades)


def _write_position_history(conn, rows: list[dict[str, Any]], *, account_code: str) -> int:
    _ensure_reference_data(conn, account_code)
    ensure_ibkr_position_history_table(conn)
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            INSERT INTO pams_ibkr_position_history (
                account_code, source_account_id, symbol, name, asset_liability,
                currency, major_category, minor_category, market_value, cost,
                quantity, buy_price, current_price, as_of
            ) VALUES (
                %(account_code)s, %(source_account_id)s, %(symbol)s, %(name)s, %(asset_liability)s,
                %(currency)s, %(major_category)s, %(minor_category)s, %(market_value)s, %(cost)s,
                %(quantity)s, %(buy_price)s, %(current_price)s, %(as_of)s
            )
            ON CONFLICT (account_code, name, as_of)
            DO UPDATE SET
                source_account_id = EXCLUDED.source_account_id,
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
            """,
            rows,
            page_size=300,
        )
    return len(rows)


def record_sync_run(
    conn,
    *,
    source: str = SYNC_SOURCE,
    status: str,
    started_at: datetime,
    as_of: date | None = None,
    statement_as_of: date | None = None,
    row_count: int = 0,
    trade_count: int = 0,
    history_row_count: int = 0,
    raw_file: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Persist one sync run status row and return it."""
    ensure_sync_table(conn)
    summary = {
        "statement_as_of": statement_as_of.isoformat() if statement_as_of else None,
        "row_count": row_count,
        "trade_count": trade_count,
        "history_row_count": history_row_count,
        "raw_file": raw_file,
        "message": message,
    }
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO pams_data_sync_runs (
                sync_key, asset_scope, target_trade_date, status, started_at,
                finished_at, latest_trade_date_after, rows_upserted,
                summary_json, error_text
            ) VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s)
            RETURNING *
            """,
            (source, "ibkr", as_of, status, started_at, as_of, row_count, Json(summary), error),
        )
        row = dict(cur.fetchone())
        cur.execute("SELECT to_regclass('public.pams_data_job_run') IS NOT NULL AS exists")
        has_job_run = bool(cur.fetchone()["exists"])
        if has_job_run:
            cur.execute(
                """
                INSERT INTO pams_data_job_run (
                    job_key, job_type, source_table, source_id, asset_scope,
                    target_date, data_as_of, status, started_at, finished_at,
                    rows_affected, summary_json, error_text
                ) VALUES (
                    %s, 'data_sync', 'pams_data_sync_runs', %s, 'ibkr',
                    %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (source_table, source_id)
                DO UPDATE SET
                    job_key = EXCLUDED.job_key,
                    job_type = EXCLUDED.job_type,
                    asset_scope = EXCLUDED.asset_scope,
                    target_date = EXCLUDED.target_date,
                    data_as_of = EXCLUDED.data_as_of,
                    status = EXCLUDED.status,
                    started_at = EXCLUDED.started_at,
                    finished_at = EXCLUDED.finished_at,
                    rows_affected = EXCLUDED.rows_affected,
                    summary_json = EXCLUDED.summary_json,
                    error_text = EXCLUDED.error_text
                """,
                (
                    source,
                    str(row.get("id")),
                    as_of,
                    statement_as_of or as_of,
                    status,
                    started_at,
                    row.get("finished_at"),
                    row_count,
                    Json(summary),
                    error,
                ),
            )
    conn.commit()
    return _sync_row_to_status(row)


def latest_sync_status(conn, *, source: str = SYNC_SOURCE) -> dict[str, Any] | None:
    """Return the latest sync run for the requested source, or `None`."""
    ensure_sync_table(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM pams_data_sync_runs
            WHERE sync_key = %s
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (source,),
        )
        row = cur.fetchone()
    return _sync_row_to_status(dict(row)) if row else None


def _sync_row_to_status(row: dict[str, Any]) -> dict[str, Any]:
    summary = row.get("summary_json") or {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "id": row.get("id"),
        "source": row.get("sync_key"),
        "asset_scope": row.get("asset_scope"),
        "status": row.get("status"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "as_of": row.get("target_trade_date"),
        "statement_as_of": summary.get("statement_as_of"),
        "row_count": row.get("rows_upserted") or summary.get("row_count") or 0,
        "trade_count": summary.get("trade_count") or 0,
        "history_row_count": summary.get("history_row_count") or 0,
        "raw_file": summary.get("raw_file"),
        "message": summary.get("message"),
        "error": row.get("error_text"),
    }


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
) -> dict[str, Any]:
    """Download one IBKR Flex report, save raw XML, and upsert PAMS holdings."""
    started_at = datetime.now(timezone.utc)
    raw_path: Path | None = None
    try:
        reference = request_flex_reference(token, query_id, flex_version=flex_version)
        xml_text = fetch_flex_statement(
            token,
            str(reference["reference_code"]),
            flex_version=flex_version,
        )
        raw_path = save_raw_flex_xml(xml_text, query_id, raw_dir=raw_dir)
        parsed = parse_flex_report(xml_text, account_code=account_code)
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
        row_count = _write_holdings(conn, parsed.rows, account_code=account_code, as_of=target_date)
        history_row_count = _write_position_history(conn, parsed.history_rows, account_code=account_code)
        trade_count = _write_trades(conn, parsed.trades, account_code=account_code)
        record = record_sync_run(
            conn,
            status="success",
            started_at=started_at,
            as_of=target_date,
            statement_as_of=parsed.statement_as_of,
            row_count=row_count,
            trade_count=trade_count,
            history_row_count=history_row_count,
            raw_file=str(raw_path),
            message=(
                f"reference={reference['reference_code']}; "
                f"accounts={','.join(parsed.source_account_ids)}; "
                f"trades={trade_count}; history_rows={history_row_count}"
            ),
        )
        return {
            "success": True,
            "status": "success",
            "as_of": target_date.isoformat(),
            "statement_as_of": parsed.statement_as_of.isoformat(),
            "row_count": row_count,
            "trade_count": trade_count,
            "history_row_count": history_row_count,
            "raw_file": str(raw_path),
            "reference_code": reference["reference_code"],
            "sync_run": record,
        }
    except Exception as exc:
        conn.rollback()
        record_sync_run(
            conn,
            status="failed",
            started_at=started_at,
            raw_file=str(raw_path) if raw_path else None,
            error=str(exc),
        )
        raise
