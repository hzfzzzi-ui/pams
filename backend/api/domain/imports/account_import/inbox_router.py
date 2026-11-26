"""Automatic file routing, SHA256 dedup scanning, maintenance tiers, and cash-yield reconciliation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

IGNORED_EXTENSIONS = {".dump", ".sha256", ".html", ".htm", ".tmp", ".part", ".ds_store", ".db"}
SUPPORTED_EXTENSIONS = {".pdf", ".xls", ".xlsx", ".csv", ".tsv", ".zip", ".png", ".jpg", ".jpeg"}

DEFAULT_MAINTENANCE_TIERS: dict[str, str] = {
    "ibkr": "auto",
    "ibkr_in_transit_mxn": "none",
    "unemployment_insurance": "none",
    "work_injury_insurance": "none",
    "maternity_insurance": "none",
    "huawei_stock": "event",
    "ccb_receivable_loan": "event",
    "ccb_gold_accumulation": "event",
    "hpf": "quarterly",
    "medical_insurance": "quarterly",
    "pension_insurance": "quarterly",
    "icbc_6689_cny": "quarterly",
    "icbc_6689_usd": "quarterly",
}


def account_maintenance_tier(account_code: str, cfg: dict[str, Any] | None = None) -> str:
    """Return maintenance cadence tier: auto | monthly | quarterly | event | none."""
    cfg = cfg or {}
    explicit = str(cfg.get("maintenance_tier") or "").strip().lower()
    if explicit in {"auto", "monthly", "quarterly", "event", "none"}:
        return explicit
    if cfg.get("active") is False or cfg.get("include_in_nav") is False:
        return "none"
    if account_code in DEFAULT_MAINTENANCE_TIERS:
        return DEFAULT_MAINTENANCE_TIERS[account_code]
    if cfg.get("truth_source") == "ibkr_flex":
        return "auto"
    if cfg.get("account_type") == "benefit":
        return "quarterly"
    return "monthly"


@dataclass(frozen=True)
class RouteDecision:
    accounts: tuple[str, ...]
    document_type: str  # "cash" | "trade" | "holding"
    source_name: str
    rule_id: str
    multi_account: bool = False


def route_file_to_accounts(filename: str, rel_path: str = "") -> RouteDecision | None:
    """Identify target subaccount(s) and document type from filename and optional relative folder path."""
    name = Path(filename).name
    ext = Path(name).suffix.lower()
    if not ext or ext in IGNORED_EXTENSIONS or ext not in SUPPORTED_EXTENSIONS:
        return None
    low_name = name.lower()
    norm_rel = rel_path.replace("\\", "/").lower()

    # 1. Santander (PDF or CSV)
    if ("santander" in low_name or "estado de cuenta" in low_name or "/santander/" in f"/{norm_rel}") and ext in {".pdf", ".csv"}:
        return RouteDecision(("santander",), "cash", "santander_statement", "santander_bill")

    # 2. Guangfa Credit Card (1 PDF splits into gf_7443 CNY + gf_9711 USD)
    if ("广发" in name or "综合对账单" in name or "/广发" in f"/{norm_rel}") and ext == ".pdf":
        return RouteDecision(("gf_7443", "gf_9711"), "cash", "gf_credit_statement", "gf_credit_pdf", multi_account=True)

    # 3. CCB 7332 Debit Card (hqmx_*.xls, *7332*.xls/pdf)
    if (low_name.startswith("hqmx_") or "7332" in low_name or "/建行储蓄卡7332/" in f"/{norm_rel}") and ext in {".xls", ".xlsx", ".pdf", ".csv"}:
        return RouteDecision(("ccb_7332",), "cash", "ccb_7332_statement", "ccb_7332_statement")

    # 4. CCB 0453 Credit Card (downfile.csv, *0453*.pdf)
    if (low_name == "downfile.csv" or "0453" in low_name or "/建行信用卡0453/" in f"/{norm_rel}") and ext in {".csv", ".pdf"}:
        return RouteDecision(("ccb_credit_0453",), "cash", "ccb_credit_0453_statement", "ccb_credit_0453_statement")

    # 5. ICBC 6689 (1 PDF splits into icbc_6689_cny + icbc_6689_usd)
    if ("工商银行" in name or "icbc" in low_name or "/工行/" in f"/{norm_rel}") and ext == ".pdf":
        return RouteDecision(("icbc_6689_cny", "icbc_6689_usd"), "cash", "icbc_statement", "icbc_pdf", multi_account=True)

    # 6. Tonghuashun (table.xls or A股_*.csv)
    if (low_name == "table.xls" or name.startswith("A股_") or "/同花顺/" in f"/{norm_rel}") and ext in {".xls", ".xlsx", ".csv"}:
        return RouteDecision(("tonghuashun",), "trade", "tonghuashun_export", "tonghuashun_export")

    # 7. WeChat Pay bill (xlsx / zip)
    if ("微信支付账单" in name or ("wechat" in low_name and ext in {".xlsx", ".csv", ".zip"})):
        return RouteDecision(("wechat",), "cash", "wechat_bill", "wechat_bill")

    # 8. Alipay bill (zip / csv)
    if ("支付宝" in name or "alipay" in low_name) and ext in {".zip", ".csv", ".xlsx"}:
        return RouteDecision(("alipay",), "cash", "alipay_bill", "alipay_bill")

    # 9. Folder/keyword-assisted routing for Tencent & Benefits screenshots/exports
    if "/活期+/" in f"/{norm_rel}" or "活期+" in name:
        return RouteDecision(("tencent_licaitong_current_plus",), "cash", "licaitong_current_plus", "licaitong_current_plus")
    if "/零钱通/" in f"/{norm_rel}" or "零钱通" in name:
        return RouteDecision(("wechat_lqt",), "cash", "wechat_lqt_snapshot", "wechat_lqt")
    if "/微信零钱/" in f"/{norm_rel}" or "零钱明细" in name:
        return RouteDecision(("wechat_balance",), "cash", "wechat_balance_snapshot", "wechat_balance")
    if "/理财通/" in f"/{norm_rel}" or "理财通" in name:
        doc_type = "holding" if ext in {".png", ".jpg", ".jpeg"} else "trade"
        return RouteDecision(("tencent_licaitong",), doc_type, "tencent_licaitong_import", "tencent_licaitong")
    if "/公积金/" in f"/{norm_rel}" or "公积金" in name:
        return RouteDecision(("hpf",), "cash", "hpf_statement", "hpf")
    if "/医保/" in f"/{norm_rel}" or "医保" in name:
        return RouteDecision(("medical_insurance",), "cash", "medical_insurance_statement", "medical_insurance")

    return None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_inbox_directory(base_dir: Path | str, known_hashes: Iterable[str] = ()) -> dict[str, Any]:
    """Scan a local dropzone directory, deduplicate by SHA256, and route new files to subaccounts."""
    root = Path(base_dir)
    if not root.exists() or not root.is_dir():
        return {"exists": False, "directory": str(root), "new_items": [], "duplicate_count": 0, "unrecognized": []}

    known = {str(h).lower() for h in known_hashes if h}
    new_items: list[dict[str, Any]] = []
    unrecognized: list[str] = []
    duplicate_count = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if not ext or ext in IGNORED_EXTENSIONS or ext not in SUPPORTED_EXTENSIONS:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        if any(part.startswith(".") or part == "PAMS备份" for part in path.relative_to(root).parts):
            continue
        digest = file_sha256(path)
        if digest in known:
            duplicate_count += 1
            continue
        decision = route_file_to_accounts(path.name, rel)
        if decision is None:
            unrecognized.append(rel)
            continue
        new_items.append({
            "rel_path": rel,
            "filename": path.name,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "accounts": list(decision.accounts),
            "document_type": decision.document_type,
            "source_name": decision.source_name,
            "rule_id": decision.rule_id,
            "multi_account": decision.multi_account,
        })

    return {
        "exists": True,
        "directory": str(root),
        "new_items": new_items,
        "duplicate_count": duplicate_count,
        "unrecognized": unrecognized,
    }


def _to_decimal(value: Any, field_name: str) -> Decimal:
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError(f"invalid decimal for {field_name}: {value}") from exc
    if not dec.is_finite():
        raise ValueError(f"non-finite decimal for {field_name}: {value}")
    return dec


def compute_cash_yield_reconciliation(
    *,
    previous_balance: Any,
    current_balance: Any,
    net_transfers: Any = "0",
) -> dict[str, Any]:
    """Compute implied monthly money-market yield from period-end balance snapshots.

    Formula: implied_yield = current_balance - (previous_balance + net_transfers)
    Eliminates manual entry of dozens of daily 0.xx CNY yield screenshots for 零钱通 / 活期+.
    """
    prev = _to_decimal(previous_balance, "previous_balance")
    curr = _to_decimal(current_balance, "current_balance")
    transfers = _to_decimal(net_transfers, "net_transfers")
    expected_without_yield = prev + transfers
    implied_yield = (curr - expected_without_yield).quantize(Decimal("0.01"))
    return {
        "previous_balance": str(prev.quantize(Decimal("0.01"))),
        "current_balance": str(curr.quantize(Decimal("0.01"))),
        "net_transfers": str(transfers.quantize(Decimal("0.01"))),
        "expected_without_yield": str(expected_without_yield.quantize(Decimal("0.01"))),
        "implied_yield": str(implied_yield),
        "needs_review": implied_yield < Decimal("0"),
    }
