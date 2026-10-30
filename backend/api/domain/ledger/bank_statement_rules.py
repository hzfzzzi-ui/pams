"""Existing bank text/amount rules shared by legacy and reviewed imports."""
from decimal import Decimal
import re
from typing import Any


def _row_transfer_amount(row: dict[str, Any]) -> Decimal:
    amount = Decimal(str(row.get("amount") or "0")).copy_abs()
    if amount != 0:
        return amount
    original_amount = row.get("original_amount")
    if original_amount is not None:
        return Decimal(str(original_amount)).copy_abs()
    raw = row.get("raw_json") or row.get("raw") or {}
    if isinstance(raw, dict) and raw.get("amount") not in (None, ""):
        return Decimal(str(raw.get("amount"))).copy_abs()
    return Decimal("0")


def _amount_close(left: Decimal, right: Decimal, tolerance: Decimal = Decimal("0.01")) -> bool:
    return (left - right).copy_abs() <= tolerance


def _classify(row: dict[str, Any], rules: list[dict[str, Any]]) -> tuple[str, str | None, bool]:
    text = " ".join(str(row.get(k) or "") for k in ("summary", "description", "counterparty"))
    for rule in rules:
        pattern = rule.get("pattern")
        if pattern and re.search(pattern, text, re.IGNORECASE):
            return str(rule.get("category") or "未分类"), rule.get("subcategory"), True
    return "未分类", None, False


def _classify_by_amount(row: dict[str, Any], rules: list[dict[str, Any]], current_category: str) -> tuple[str, str | None, bool] | None:
    amount = _row_transfer_amount(row)
    for rule in rules:
        override_categories = rule.get("override_categories") or ["未分类"]
        if current_category not in override_categories:
            continue
        max_abs_exclusive = rule.get("max_abs_exclusive")
        if max_abs_exclusive not in (None, "") and amount < Decimal(str(max_abs_exclusive)):
            return str(rule.get("category") or "未分类"), rule.get("subcategory"), True
        for value in rule.get("exact_abs_amounts") or []:
            if _amount_close(amount, Decimal(str(value))):
                return str(rule.get("category") or "未分类"), rule.get("subcategory"), True
    return None


def _classify_statement_row(row: dict[str, Any], rules: list[dict[str, Any]], amount_rules: list[dict[str, Any]]) -> tuple[str, str | None, bool]:
    category, subcategory, auto_classified = _classify(row, rules)
    amount_match = _classify_by_amount(row, amount_rules, category)
    return amount_match if amount_match else (category, subcategory, auto_classified)
