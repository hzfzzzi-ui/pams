"""
Tushare ts_code ↔ 内部 symbol 格式转换。

Tushare: 000001.SZ, 600519.SH
内部:    sz000001, sh600519
"""


def ts_to_symbol(ts_code: str) -> str:
    """000001.SZ → sz000001"""
    code, exchange = ts_code.split(".")
    return f"{exchange.lower()}{code}"


def symbol_to_ts(symbol: str) -> str:
    """sz000001 → 000001.SZ"""
    prefix = symbol[:2]
    code = symbol[2:]
    return f"{code}.{prefix.upper()}"


def exchange_from_ts(ts_code: str) -> str:
    """000001.SZ → SZSE, 600519.SH → SSE"""
    suffix = ts_code.split(".")[-1]
    return {"SZ": "SZSE", "SH": "SSE", "BJ": "BSE"}.get(suffix, suffix)
