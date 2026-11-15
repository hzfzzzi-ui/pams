"""Resolve transaction labels from the asset master and dated name evidence."""

from typing import Any

from domain.assets.alias_policy import identity_alias_sql


def _text(value: Any) -> str:
    return str(value or "").strip()


def _symbol_digits(value: Any) -> str:
    return "".join(ch for ch in _text(value) if ch.isdigit())


def _name_needs_enrichment(symbol: Any, name: Any) -> bool:
    text = _text(name)
    if not text:
        return True
    symbol_text = _text(symbol)
    if symbol_text and text.casefold() == symbol_text.casefold():
        return True
    digits = _symbol_digits(symbol) or _symbol_digits(text)
    if len(digits) == 6:
        exchange = "SH" if digits.startswith("6") else "SZ"
        raw = text.upper()
        code_like = {
            digits,
            f"{exchange}{digits}",
            f"{digits}.{exchange}",
            f"{digits}.{exchange.lower()}",
            f"SSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
            f"SHSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
        }
        if raw in {item.upper() for item in code_like}:
            return True
    return any(token in text for token in ("�", "Ã", "Â"))


def _symbol_name_keys(symbol: Any) -> set[str]:
    raw = _text(symbol)
    if not raw:
        return set()
    keys = {raw, raw.upper(), raw.lower()}
    digits = _symbol_digits(raw)
    if len(digits) == 6:
        exchange = "SH" if digits.startswith("6") else "SZ"
        keys.update(
            {
                digits,
                f"{digits}.{exchange}",
                f"{digits}.{exchange.lower()}",
                f"{exchange.lower()}{digits}",
                f"{exchange}{digits}",
                f"SSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
                f"SHSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
            }
        )
    return {key.casefold() for key in keys if key}


def _put_asset_name(lookup: dict[str, str], symbol: Any, name: Any) -> None:
    label = _text(name)
    if not label or _name_needs_enrichment(symbol, label):
        return
    for key in _symbol_name_keys(symbol):
        lookup.setdefault(key, label)


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
    row = cur.fetchone()
    return bool(row and row.get("exists"))


def _asset_name_lookup(cur, symbols: set[str], *, fallback=None) -> dict[str, str]:
    lookup: dict[str, str] = {}
    query_symbols = sorted(set().union(*(_symbol_name_keys(symbol) for symbol in symbols)))
    digits = sorted({_symbol_digits(symbol) for symbol in symbols if len(_symbol_digits(symbol)) == 6})
    if not query_symbols and not digits:
        return lookup

    if _table_exists(cur, "pams_asset_master"):
        cur.execute(
            """
            SELECT primary_symbol AS symbol, COALESCE(NULLIF(display_name, ''), canonical_name) AS name
            FROM pams_asset_master
            WHERE COALESCE(primary_symbol, '') <> ''
              AND COALESCE(COALESCE(NULLIF(display_name, ''), canonical_name), '') <> ''
              AND (
                LOWER(primary_symbol) = ANY(%s::text[])
                OR regexp_replace(COALESCE(primary_symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY confidence DESC NULLS LAST, updated_at DESC NULLS LAST
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    if _table_exists(cur, "pams_asset_alias") and _table_exists(cur, "pams_asset_master"):
        cur.execute(
            f"""
            SELECT al.alias_value AS symbol, COALESCE(NULLIF(am.display_name, ''), am.canonical_name) AS name
            FROM pams_asset_alias al
            JOIN pams_asset_master am ON am.asset_id = al.asset_id
            WHERE {identity_alias_sql('al')}
              AND COALESCE(al.alias_value, '') <> ''
              AND COALESCE(COALESCE(NULLIF(am.display_name, ''), am.canonical_name), '') <> ''
              AND (
                LOWER(al.alias_value) = ANY(%s::text[])
                OR regexp_replace(COALESCE(al.alias_value, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY al.priority DESC NULLS LAST, al.created_at DESC NULLS LAST
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    if _table_exists(cur, "pams_holdings_snapshot"):
        cur.execute(
            """
            SELECT symbol, name
            FROM pams_holdings_snapshot
            WHERE COALESCE(name, '') <> ''
              AND (
                LOWER(symbol) = ANY(%s::text[])
                OR regexp_replace(COALESCE(symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY as_of DESC
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    if _table_exists(cur, "pit_security_master"):
        cur.execute(
            """
            SELECT symbol, ts_code, name
            FROM pit_security_master
            WHERE COALESCE(name, '') <> ''
              AND (
                LOWER(symbol) = ANY(%s::text[])
                OR LOWER(ts_code) = ANY(%s::text[])
                OR regexp_replace(COALESCE(symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
                OR regexp_replace(COALESCE(ts_code, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            """,
            (query_symbols, query_symbols, digits, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))
            _put_asset_name(lookup, row.get("ts_code"), row.get("name"))

    for symbol, name in (fallback or {}).items():
        if symbol in digits:
            _put_asset_name(lookup, symbol, name)
    return lookup
