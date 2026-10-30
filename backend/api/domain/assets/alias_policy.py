"""Exclude precisely identified legacy aliases from asset identity resolution.

These rows remain in the database for separately authorized correction. A
lookthrough proxy is not an identity alias; no new asset identity is inferred.
"""

QQQ_ASSET_ID = "ETF_QQQ"
IDENTITY_ALIAS_TYPE = "alias"
QQQ_FEEDER_NAMES = (
    "华泰柏瑞纳斯达克100ETF发起式联接(QDII)I",
    "广发纳斯达克100ETF联接人民币(QDII)A",
    "建信纳斯达克100指数(QDII)A人民币",
    "建信纳斯达克100指数(QDII)C人民币",
)


def is_identity_alias(asset_id: str | None, alias_type: str | None, alias_value: str | None) -> bool:
    return not (asset_id == QQQ_ASSET_ID and alias_type == IDENTITY_ALIAS_TYPE and alias_value in QQQ_FEEDER_NAMES)


def identity_alias_sql(alias: str) -> str:
    """Return a SQL predicate for a fixed, internal table alias only."""
    if alias not in {"aa", "al"}:
        raise ValueError("Unknown asset alias SQL identifier")
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in QQQ_FEEDER_NAMES)
    return f"({alias}.asset_id = '{QQQ_ASSET_ID}' AND {alias}.alias_type = '{IDENTITY_ALIAS_TYPE}' AND {alias}.alias_value IN ({names})) IS NOT TRUE"
