"""Import form compatibility with existing asset-master vocabulary, not a new taxonomy."""


def asset_type(value: str | None) -> str:
    value = str(value or "").lower().strip()
    if value in {"fund", "etf", "mutual_fund", "基金", "etf / 基金", "债券 / 货币基金"}:
        return "fund"
    if value in {"stock", "equity", "listed_equity", "股票", "a股", "a股个股"}:
        return "stock"
    return value
