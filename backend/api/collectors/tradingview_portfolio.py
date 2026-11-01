"""TradingView portfolio CSV importer for local A-share holdings.

The importer consumes user-exported CSV files only. It does not store
TradingView credentials, does not call TradingView APIs, and does not submit
orders.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import psycopg2
import psycopg2.extras


DEFAULT_ACCOUNT_CODE = "tonghuashun"
DEFAULT_RAW_SUBDIR = Path("raw") / "tradingview" / "a_share"
DEFAULT_ACCOUNT_LEDGER_ROOT = Path("/host/account-ledger")
DEFAULT_TONGHUASHUN_LEDGER_NAME = "\u540c\u82b1\u987a"
SUPPORTED_SIDES = {"Buy", "Sell", "Deposit", "Dividend"}

A_SHARE_NAME_MAP = {
    "000630": "铜陵有色",
    "000735": "罗 牛 山",
    "002185": "华天科技",
    "600487": "亨通光电",
    "600550": "保变电气",
    "600584": "长电科技",
    "601872": "招商轮船",
    "601899": "紫金矿业",
}


class TradingViewImportError(RuntimeError):
    """Raised when a TradingView CSV cannot be parsed or persisted safely."""


@dataclass(frozen=True)
class TradingViewTransaction:
    """One normalized row from a TradingView portfolio transaction CSV."""

    source_file: str
    source_hash: str
    row_hash: str
    symbol: str
    normalized_symbol: str | None
    side: str
    quantity: Decimal
    fill_price: Decimal | None
    commission: Decimal
    closing_time: datetime
    raw: dict[str, str]


@dataclass(frozen=True)
class MarketMark:
    """Current mark used to value one position."""

    price: Decimal
    name: str | None = None
    unrealized_pnl: Decimal | None = None


@dataclass(frozen=True)
class PortfolioPosition:
    """Computed open position for one security."""

    symbol: str
    tradingview_symbol: str
    name: str
    quantity: Decimal
    current_price: Decimal
    market_value: Decimal
    cost: Decimal | None
    buy_price: Decimal | None
    holding_pnl: Decimal | None


@dataclass(frozen=True)
class PortfolioCalcResult:
    """Portfolio valuation computed from transactions plus current marks."""

    as_of: date
    account_code: str
    positions: list[PortfolioPosition]
    cash: Decimal | None
    total_deposits: Decimal
    total_dividends: Decimal | None
    holdings_market_value: Decimal
    holdings_cost: Decimal | None
    holding_pnl: Decimal | None
    total_nav: Decimal | None
    realized_pnl: Decimal | None
    raw_transaction_count: int
    deduped_transaction_count: int


def _data_dir() -> Path:
    return Path(os.environ.get("PAMS_DATA_DIR", "data"))


def default_raw_dir() -> Path:
    """Return the default raw archive directory for TradingView A-share files."""

    return _data_dir() / DEFAULT_RAW_SUBDIR


def default_ledger_dirs() -> list[Path]:
    """Return local account-ledger directories used as A-share transaction sources."""

    dirs = [
        DEFAULT_ACCOUNT_LEDGER_ROOT / DEFAULT_TONGHUASHUN_LEDGER_NAME,
        Path("\u8d26\u6237\u53f0\u8d26") / DEFAULT_TONGHUASHUN_LEDGER_NAME,
    ]
    return [path for path in dirs if path.exists()]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _to_decimal(value: Any, *, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "N/A", "nan", "NaN", "None"}:
        return default
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise TradingViewImportError(f"Invalid numeric value: {value!r}") from exc


def _parse_time(value: str) -> datetime:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise TradingViewImportError(f"Invalid TradingView Closing Time: {value!r}")


def normalize_symbol(symbol: str) -> str | None:
    """Normalize TradingView symbols to six-digit A-share display codes."""

    text = str(symbol or "").strip()
    if not text or text == "$CASH":
        return None
    if ":" in text:
        return text.split(":", 1)[1].strip()
    if "." in text:
        return text.split(".", 1)[0].strip()
    return text


def _row_hash(row: dict[str, str]) -> str:
    keys = ["Symbol", "Side", "Qty", "Fill Price", "Commission", "Closing Time"]
    payload = {key: str(row.get(key, "")).strip() for key in keys}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_transactions_csv(path: str | Path) -> list[TradingViewTransaction]:
    """Parse one TradingView transaction CSV into normalized transaction rows."""

    csv_path = Path(path)
    data = csv_path.read_bytes()
    source_hash = _sha256_bytes(data)
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    required = {"Symbol", "Side", "Qty", "Fill Price", "Commission", "Closing Time"}
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise TradingViewImportError(f"{csv_path.name} missing columns: {sorted(missing)}")

    rows: list[TradingViewTransaction] = []
    for raw in reader:
        side = str(raw.get("Side", "")).strip()
        if side not in SUPPORTED_SIDES:
            raise TradingViewImportError(f"Unsupported TradingView transaction side: {side!r}")
        quantity = _to_decimal(raw.get("Qty"), default=Decimal("0"))
        if quantity is None:
            quantity = Decimal("0")
        fill_price = _to_decimal(raw.get("Fill Price"), default=None)
        commission = _to_decimal(raw.get("Commission"), default=Decimal("0")) or Decimal("0")
        symbol = str(raw.get("Symbol", "")).strip()
        row = TradingViewTransaction(
            source_file=csv_path.name,
            source_hash=source_hash,
            row_hash=_row_hash(raw),
            symbol=symbol,
            normalized_symbol=normalize_symbol(symbol),
            side=side,
            quantity=quantity,
            fill_price=fill_price,
            commission=commission,
            closing_time=_parse_time(str(raw.get("Closing Time", ""))),
            raw={k: "" if v is None else str(v) for k, v in raw.items()},
        )
        rows.append(row)
    return rows


def read_transactions(paths: Iterable[str | Path]) -> tuple[list[TradingViewTransaction], int]:
    """Read one or more CSV files and de-duplicate repeated exported rows."""

    all_rows: list[TradingViewTransaction] = []
    for path in paths:
        all_rows.extend(parse_transactions_csv(path))
    deduped: dict[str, TradingViewTransaction] = {}
    for row in all_rows:
        deduped.setdefault(row.row_hash, row)
    ordered = sorted(deduped.values(), key=lambda r: (r.closing_time, r.row_hash))
    return ordered, len(all_rows)


def archive_source_files(paths: Iterable[str | Path], raw_dir: str | Path | None = None) -> list[Path]:
    """Copy TradingView source CSV files into the local raw-data archive."""

    archive_dir = Path(raw_dir) if raw_dir else default_raw_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived: list[Path] = []
    for source in paths:
        src = Path(source)
        dst = archive_dir / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        archived.append(dst)
    return archived


def load_market_marks(path: str | Path) -> tuple[date | None, dict[str, MarketMark]]:
    """Load position marks from JSON.

    Supported shape:
    {
      "as_of": "2026-05-31",
      "marks": {
        "SSE:601899": {"price": 30.44, "unrealized_pnl": -18537, "name": "紫金矿业"}
      }
    }
    A simpler {"prices": {"SSE:601899": 30.44}} shape is also accepted.
    """

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    as_of = None
    if payload.get("as_of"):
        as_of = date.fromisoformat(str(payload["as_of"]))

    source = payload.get("marks") or payload.get("prices") or {}
    marks: dict[str, MarketMark] = {}
    for symbol, value in source.items():
        if isinstance(value, dict):
            price = _to_decimal(value.get("price"), default=None)
            pnl = _to_decimal(value.get("unrealized_pnl"), default=None)
            name = value.get("name")
        else:
            price = _to_decimal(value, default=None)
            pnl = None
            name = None
        if price is None:
            raise TradingViewImportError(f"Missing mark price for {symbol}")
        mark = MarketMark(price=price, name=str(name).strip() if name else None, unrealized_pnl=pnl)
        keys = {str(symbol), normalize_symbol(str(symbol)) or str(symbol)}
        for key in keys:
            marks[key] = mark
    return as_of, marks


def _pop_lot(lots: list[list[Decimal]], quantity: Decimal) -> Decimal:
    remaining = quantity
    cost = Decimal("0")
    while remaining > 0 and lots:
        lot_qty, lot_unit_cost = lots[0]
        take = min(remaining, lot_qty)
        cost += take * lot_unit_cost
        lot_qty -= take
        remaining -= take
        if lot_qty == 0:
            lots.pop(0)
        else:
            lots[0][0] = lot_qty
    if remaining > 0:
        raise TradingViewImportError(f"Sell quantity exceeds open lots by {remaining}")
    return cost


def calculate_portfolio(
    transactions: list[TradingViewTransaction],
    marks: dict[str, MarketMark],
    *,
    as_of: date,
    account_code: str = DEFAULT_ACCOUNT_CODE,
    require_explicit_tax: bool = False,
) -> PortfolioCalcResult:
    """Calculate cash, open holdings, NAV, and PnL from transactions and marks."""

    lots: dict[str, list[list[Decimal]]] = {}
    cash = Decimal("0")
    deposits = Decimal("0")
    dividends = Decimal("0")
    fifo_realized = Decimal("0")
    unknown_tax_symbols: set[str] = set()

    for tx in transactions:
        tax = Decimal('0')
        if require_explicit_tax and tx.side in {'Buy', 'Sell', 'Dividend'}:
            text = str(tx.raw.get('Tax') or '').strip()
            tax_value = _to_decimal(text, default=None) if text else None
            if tax_value is None:
                tax = None
                cash = None
                fifo_realized = None
                if tx.normalized_symbol and tx.side in {'Buy','Sell'}:
                    unknown_tax_symbols.add(tx.normalized_symbol)
                if tx.side == 'Dividend':
                    dividends = None
            elif not tax_value.is_finite() or tax_value < 0:
                raise TradingViewImportError('TradingView tax is invalid')
            else:
                tax = tax_value
        if tx.side == "Deposit":
            if cash is not None:
                cash += tx.quantity
            deposits += tx.quantity
            continue
        if tx.side == "Dividend":
            if tax is not None:
                dividend_net = tx.quantity - tax - (tx.commission if require_explicit_tax else Decimal('0'))
                if cash is not None:
                    cash += dividend_net
                if dividends is not None:
                    dividends += dividend_net
            continue
        if tx.fill_price is None:
            raise TradingViewImportError(f"{tx.side} row is missing Fill Price for {tx.symbol}")
        if not tx.normalized_symbol:
            raise TradingViewImportError(f"{tx.side} row is missing a security symbol")

        gross = tx.quantity * tx.fill_price
        symbol = tx.normalized_symbol
        if tx.side == "Buy":
            if cash is not None and tax is not None:
                cash -= gross + tx.commission + tax
            lots.setdefault(symbol, []).append([tx.quantity, tx.fill_price])
        elif tx.side == "Sell":
            if cash is not None and tax is not None:
                cash += gross - tx.commission - tax
            closed_cost = _pop_lot(lots.setdefault(symbol, []), tx.quantity)
            if fifo_realized is not None and tax is not None:
                fifo_realized += gross - tx.commission - tax - closed_cost

    positions: list[PortfolioPosition] = []
    for symbol, symbol_lots in lots.items():
        quantity = sum((lot[0] for lot in symbol_lots), Decimal("0"))
        if quantity == 0:
            continue
        lot_cost = sum((lot[0] * lot[1] for lot in symbol_lots), Decimal("0"))
        mark = marks.get(symbol)
        if mark is None:
            prefixed = f"SSE:{symbol}" if symbol.startswith("6") else f"SZSE:{symbol}"
            mark = marks.get(prefixed)
        if mark is None:
            raise TradingViewImportError(f"Missing current market mark for open position {symbol}")

        market_value = quantity * mark.price
        cost = None if symbol in unknown_tax_symbols else (market_value - mark.unrealized_pnl if mark.unrealized_pnl is not None else lot_cost)
        buy_price = cost / quantity if cost is not None and quantity else None
        tradingview_symbol = f"SSE:{symbol}" if symbol.startswith("6") else f"SZSE:{symbol}"
        positions.append(
            PortfolioPosition(
                symbol=symbol,
                tradingview_symbol=tradingview_symbol,
                name=mark.name or A_SHARE_NAME_MAP.get(symbol, symbol),
                quantity=quantity,
                current_price=mark.price,
                market_value=market_value,
                cost=cost,
                buy_price=buy_price,
                holding_pnl=market_value - cost if cost is not None else None,
            )
        )

    positions.sort(key=lambda p: p.market_value, reverse=True)
    holdings_market_value = sum((p.market_value for p in positions), Decimal("0"))
    holdings_cost = None if any(p.cost is None for p in positions) else sum((p.cost for p in positions), Decimal("0"))
    holding_pnl = holdings_market_value - holdings_cost if holdings_cost is not None else None
    total_nav = holdings_market_value + cash if cash is not None else None
    realized = (total_nav - deposits - holding_pnl if total_nav is not None and holding_pnl is not None else None) if positions else fifo_realized

    return PortfolioCalcResult(
        as_of=as_of,
        account_code=account_code,
        positions=positions,
        cash=cash,
        total_deposits=deposits,
        total_dividends=dividends,
        holdings_market_value=holdings_market_value,
        holdings_cost=holdings_cost,
        holding_pnl=holding_pnl,
        total_nav=total_nav,
        realized_pnl=realized,
        raw_transaction_count=len(transactions),
        deduped_transaction_count=len(transactions),
    )


def _money(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.0001")) if value is not None else None


def _price(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.000001")) if value is not None else None


def get_conn():
    """Create a database connection using the same environment variables as pams-api."""

    from domain.imports.isolation import guarded_connect

    return guarded_connect(
        host=os.environ.get("PGHOST", "postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def sync_portfolio_to_db(
    conn,
    result: PortfolioCalcResult,
    transactions: list[TradingViewTransaction],
) -> None:
    """Write holdings only after each input trade has a verified source fact."""

    from domain.investments import holding_write_evidence as evidence
    if not evidence.enabled(conn):
        raise TradingViewImportError('TradingView sync requires isolated source evidence')
    evidence.lock_account(conn, result.account_code)
    names = [position.name for position in result.positions]
    if result.cash is not None and result.cash != 0:
        names.append('同花顺现金')
    if not names or len(set(names)) != len(names):
        raise ValueError('holding result set is empty or ambiguous')
    evidence.reject_unproven_retirements(conn, account_code=result.account_code, business_date=result.as_of,
                                         names=names, origin='tradingview', cash_unknown=result.cash is None)
    identities = [{'name': position.name, 'symbol': position.symbol, 'currency': 'CNY'} for position in result.positions]
    if result.cash is not None and result.cash != 0:
        identities.append({'name': '同花顺现金', 'symbol': '$CASH', 'currency': 'CNY'})
    evidence.reject_identity_conflicts(conn, account_code=result.account_code, business_date=result.as_of, incoming=identities)
    proven_trade_ids = evidence.reject_unproven_tradingview_collisions(conn, account_code=result.account_code, transactions=transactions)
    from domain.imports.tradingview_source_publish import source_row_from_item
    expected_cash: Decimal | None = Decimal('0')
    expected_quantities: dict[str, Decimal] = {}
    for tx in transactions:
        fact = source_row_from_item({'account_code': result.account_code},
            {'source_item_id': '00000000-0000-0000-0000-000000000000', 'source_uid': 'validation'},
            {'filename': tx.source_file}, tx.raw)
        tax = fact['tax_amount']
        if tx.side in {'Buy','Sell'}:
            if tx.fill_price is None:
                raise TradingViewImportError('TradingView price is unknown')
            symbol = tx.normalized_symbol
            if not symbol:
                raise TradingViewImportError('TradingView security identity is unknown')
            prior = expected_quantities.get(symbol, Decimal('0'))
            current = prior + (tx.quantity if tx.side == 'Buy' else -tx.quantity)
            if current < 0:
                raise TradingViewImportError('TradingView sale exceeds proven holdings')
            expected_quantities[symbol] = current
        if tx.side in {'Buy', 'Sell', 'Dividend'} and tax is None:
            expected_cash = None
            if tx.normalized_symbol and tx.side in {'Buy','Sell'}:
                affected = [position for position in result.positions if position.symbol == tx.normalized_symbol]
                if any(position.cost is not None or position.buy_price is not None or position.holding_pnl is not None
                       for position in affected):
                    raise TradingViewImportError('TradingView holding cost requires confirmed taxes')
        if expected_cash is not None:
            if tx.side == 'Deposit':
                expected_cash += tx.quantity
            elif tx.side == 'Dividend':
                expected_cash += tx.quantity - tx.commission - tax
            elif tx.side == 'Buy':
                expected_cash -= tx.quantity * tx.fill_price + tx.commission + tax
            elif tx.side == 'Sell':
                expected_cash += tx.quantity * tx.fill_price - tx.commission - tax
    expected_open = {symbol:quantity for symbol,quantity in expected_quantities.items() if quantity != 0}
    if expected_open:
        with conn.cursor() as cur:
            cur.execute("""SELECT trade_id FROM pams_investment_trade_fact WHERE account_code=%s
                AND trade_date<=%s AND symbol=ANY(%s::text[])
                AND trade_type IN ('buy','sell','fund_subscription','fund_redemption')
                AND COALESCE(review_status,'') NOT IN ('ignored','cancelled','canceled','void','duplicate')""",
                (result.account_code,result.as_of,list(expected_open)))
            existing_trade_ids = {row[0] for row in cur.fetchall()}
        if not existing_trade_ids.issubset(proven_trade_ids):
            raise TradingViewImportError('TradingView holding has other recorded trade inputs requiring review')
    actual_open = {position.symbol:position.quantity for position in result.positions}
    if (len(actual_open) != len(result.positions) or actual_open != expected_open or
        any(not position.current_price.is_finite() or position.current_price <= 0 or
            position.market_value != position.quantity * position.current_price for position in result.positions)):
        raise TradingViewImportError('TradingView holding quantity or valuation differs from proven inputs')
    if result.cash != expected_cash:
        raise TradingViewImportError('TradingView calculated cash does not include confirmed taxes')
    if expected_cash is None and (result.total_nav is not None or result.realized_pnl is not None):
        raise TradingViewImportError('TradingView account totals require confirmed taxes')
    try:
        evidence.capture_existing_holdings(conn, account_code=result.account_code, business_date=result.as_of, names=names)
        def finish():
            evidence.capture_written_holdings(conn, account_code=result.account_code, business_date=result.as_of,
                                              names=names, origin='tradingview', transactions=transactions)
            from domain.investments.account_snapshot_evidence import record_partial
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM pams_holdings_snapshot WHERE account_code=%s AND as_of=%s AND name=ANY(%s::text[]) ORDER BY id",
                            (result.account_code,result.as_of,names))
                identifiers = [row[0] for row in cur.fetchall()]
            record_partial(conn, account_code=result.account_code, as_of=result.as_of, holding_ids=identifiers,
                           reason='tradingview input coverage and account cash are not independently complete',
                           calculation_version='tradingview-partial-v1')
        return _write_portfolio_holdings(conn, result, before_commit=finish)
    except Exception:
        conn.rollback()
        raise


def _write_portfolio_holdings(conn, result: PortfolioCalcResult, *, before_commit) -> None:
    """Write verified holdings without recreating a legacy transaction source."""
    from domain.investments import holding_write_evidence as evidence
    if not evidence.enabled(conn):
        raise TradingViewImportError('TradingView sync requires isolated source evidence')
    evidence.lock_account(conn, result.account_code)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pams_accounts (account_code, display_name, base_currency, is_asset, active)
            VALUES (%s, %s, 'CNY', true, true)
            ON CONFLICT (account_code) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                base_currency = EXCLUDED.base_currency,
                is_asset = EXCLUDED.is_asset,
                active = EXCLUDED.active
            """,
            (result.account_code, "同花顺"),
        )
        cur.execute("SELECT to_regclass('public.pams_asset_categories')")
        if cur.fetchone()[0] is not None:
            cur.execute(
                """
                INSERT INTO pams_asset_categories (major_category, minor_category)
                VALUES ('A股', 'A股'), ('现金', '现金/活期')
                ON CONFLICT (major_category, minor_category) DO NOTHING
                """
            )
        cur.execute(
            """
            INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
            VALUES (%s, 'CNY', 'CNY', 1, 'tradingview_import')
            ON CONFLICT (as_of, base_currency, quote_currency)
            DO UPDATE SET rate = EXCLUDED.rate, source = EXCLUDED.source
            """,
            (result.as_of,),
        )

        holding_rows = [
            (
                p.symbol,
                p.name,
                "资产",
                "CNY",
                result.account_code,
                "A股",
                "A股",
                _money(p.market_value),
                _money(p.cost),
                result.as_of,
                _price(p.quantity),
                _price(p.buy_price),
                _price(p.current_price),
            )
            for p in result.positions
        ]
        if result.cash is not None and result.cash != 0:
            holding_rows.append(
                (
                    "$CASH",
                    "同花顺现金",
                    "资产",
                    "CNY",
                    result.account_code,
                    "现金",
                    "现金/活期",
                    _money(result.cash),
                    _money(result.cash),
                    result.as_of,
                    _price(result.cash),
                    Decimal("1.000000"),
                    Decimal("1.000000"),
                )
            )
        unchanged_guard = """WHERE (pams_holdings_snapshot.symbol,pams_holdings_snapshot.asset_liability,
            pams_holdings_snapshot.currency,pams_holdings_snapshot.major_category,pams_holdings_snapshot.minor_category,
            pams_holdings_snapshot.market_value,pams_holdings_snapshot.cost,pams_holdings_snapshot.quantity,
            pams_holdings_snapshot.buy_price,pams_holdings_snapshot.current_price)
            IS DISTINCT FROM (EXCLUDED.symbol,EXCLUDED.asset_liability,EXCLUDED.currency,EXCLUDED.major_category,
            EXCLUDED.minor_category,EXCLUDED.market_value,EXCLUDED.cost,EXCLUDED.quantity,EXCLUDED.buy_price,EXCLUDED.current_price)"""
        psycopg2.extras.execute_values(
            cur,
            f"""
            INSERT INTO pams_holdings_snapshot (
                symbol, name, asset_liability, currency, account_code,
                major_category, minor_category, market_value, cost, as_of,
                quantity, buy_price, current_price
            )
            VALUES %s
            ON CONFLICT (account_code, name, as_of) DO UPDATE SET
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
            holding_rows,
        )
    before_commit()
    conn.commit()


def summarize_result(result: PortfolioCalcResult) -> dict[str, Any]:
    """Return a JSON-serializable summary of a TradingView portfolio valuation."""

    def money(value):
        return str(value.quantize(Decimal("0.01"))) if value is not None else None

    return {
        "as_of": result.as_of.isoformat(),
        "account_code": result.account_code,
        "positions_count": len(result.positions),
        "cash": money(result.cash),
        "holdings_market_value": money(result.holdings_market_value),
        "holdings_cost": money(result.holdings_cost),
        "holding_pnl": money(result.holding_pnl),
        "total_nav": money(result.total_nav),
        "realized_pnl": money(result.realized_pnl),
        "total_deposits": money(result.total_deposits),
        "total_dividends": money(result.total_dividends),
        "positions": [
            {
                "symbol": p.symbol,
                "name": p.name,
                "quantity": str(p.quantity.normalize()),
                "buy_price": str(p.buy_price.quantize(Decimal("0.0001"))) if p.buy_price is not None else None,
                "current_price": str(p.current_price.quantize(Decimal("0.0001"))),
                "market_value": str(p.market_value.quantize(Decimal("0.01"))),
                "cost": money(p.cost),
                "holding_pnl": money(p.holding_pnl),
            }
            for p in result.positions
        ],
    }


def _collect_default_csvs(raw_dir: Path) -> list[Path]:
    source_dirs = [raw_dir, *default_ledger_dirs()]
    csvs = sorted(
        {csv.resolve(): csv for source_dir in source_dirs for csv in source_dir.glob("*.csv")}.values(),
        key=lambda path: (path.name, str(path)),
    )
    if not csvs:
        source_text = ", ".join(str(path) for path in source_dirs)
        raise TradingViewImportError(f"No TradingView CSV files found in {source_text}")
    return csvs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import TradingView A-share CSV transactions into PAMS.")
    parser.add_argument(
        "csv",
        nargs="*",
        help="TradingView CSV files. Defaults to data/raw/tradingview/a_share and account-ledger/tonghuashun CSVs.",
    )
    parser.add_argument("--marks-json", help="JSON file with current prices and optional unrealized PnL")
    parser.add_argument("--as-of", help="Snapshot date, defaults to marks JSON as_of or today")
    parser.add_argument("--account-code", default=DEFAULT_ACCOUNT_CODE)
    parser.add_argument("--raw-dir", default=None)
    parser.add_argument("--no-archive", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    raw_dir = Path(args.raw_dir) if args.raw_dir else default_raw_dir()
    input_paths = [Path(p) for p in args.csv] if args.csv else _collect_default_csvs(raw_dir)
    if not args.no_archive:
        input_paths = archive_source_files(input_paths, raw_dir)

    marks_path = Path(args.marks_json) if args.marks_json else raw_dir / "A股_2026-05-31_marks_from_screenshot.json"
    marks_as_of, marks = load_market_marks(marks_path)
    as_of = date.fromisoformat(args.as_of) if args.as_of else marks_as_of or date.today()
    transactions, raw_count = read_transactions(input_paths)
    conn = None
    try:
        if not args.dry_run:
            conn = get_conn()
        result = calculate_portfolio(transactions, marks, as_of=as_of, account_code=args.account_code,
                                     require_explicit_tax=True)
        result = PortfolioCalcResult(
            **{**result.__dict__, "raw_transaction_count": raw_count, "deduped_transaction_count": len(transactions)}
        )
        if conn is not None:
            sync_portfolio_to_db(conn, result, transactions)
        summary = summarize_result(result)
        summary["raw_transaction_count"] = raw_count
        summary["deduped_transaction_count"] = len(transactions)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
