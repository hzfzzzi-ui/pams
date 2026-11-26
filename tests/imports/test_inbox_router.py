"""Unit tests for inbox_router: file-to-subaccount routing, SHA256 dedup, tiers, and cash yield diff."""

from pathlib import Path

from domain.imports.account_import.inbox_router import (
    account_maintenance_tier,
    compute_cash_yield_reconciliation,
    route_file_to_accounts,
    scan_inbox_directory,
)


def test_routes_real_files_from_user_ledger_patterns():
    r1 = route_file_to_accounts("2026-10_Santander.pdf", "santander/2026-10/2026-10_Santander.pdf")
    assert r1 is not None and r1.accounts == ("santander",) and r1.document_type == "cash"

    r2 = route_file_to_accounts("2026-11_广发银行信用卡.pdf", "广发银行/2026-11/2026-11_广发银行信用卡.pdf")
    assert r2 is not None and r2.accounts == ("gf_7443", "gf_9711") and r2.multi_account is True

    r3 = route_file_to_accounts("hqmx_20261102071712.xls", "中国建设银行/建行储蓄卡7332/hqmx_20261102071712.xls")
    assert r3 is not None and r3.accounts == ("ccb_7332",)

    r4 = route_file_to_accounts("downfile.csv", "中国建设银行/建行信用卡0453/downfile.csv")
    assert r4 is not None and r4.accounts == ("ccb_credit_0453",)

    r5 = route_file_to_accounts("table.xls", "同花顺/table.xls")
    assert r5 is not None and r5.accounts == ("tonghuashun",) and r5.document_type == "trade"

    r6 = route_file_to_accounts("工商银行历史明细（申请单号：260531）.pdf", "工行/工商银行历史明细（申请单号：260531）.pdf")
    assert r6 is not None and r6.accounts == ("icbc_6689_cny", "icbc_6689_usd") and r6.multi_account is True

    r7 = route_file_to_accounts("微信支付账单流水文件(20260101-20260531).xlsx")
    assert r7 is not None and r7.accounts == ("wechat",)

    r8 = route_file_to_accounts("支付宝交易明细(20260101-20260531).zip")
    assert r8 is not None and r8.accounts == ("alipay",)

    r9 = route_file_to_accounts("IMG_3186.PNG", "腾讯/活期+/IMG_3186.PNG")
    assert r9 is not None and r9.accounts == ("tencent_licaitong_current_plus",)

    r10 = route_file_to_accounts("IMG_3136.PNG", "腾讯/零钱通/IMG_3136.PNG")
    assert r10 is not None and r10.accounts == ("wechat_lqt",)

    assert route_file_to_accounts("pams_v4.dump", "PAMS备份/pams_v4.dump") is None


def test_scan_inbox_directory_deduplicates_by_sha256(tmp_path: Path):
    (tmp_path / "santander").mkdir()
    f1 = tmp_path / "santander" / "2026-10_Santander.pdf"
    f1.write_bytes(b"%PDF-1.4 sample santander")
    (tmp_path / "PAMS备份").mkdir()
    (tmp_path / "PAMS备份" / "backup.dump").write_bytes(b"dump")

    first = scan_inbox_directory(tmp_path)
    assert first["exists"] is True
    assert len(first["new_items"]) == 1
    assert first["duplicate_count"] == 0
    digest = first["new_items"][0]["sha256"]

    second = scan_inbox_directory(tmp_path, known_hashes={digest})
    assert len(second["new_items"]) == 0
    assert second["duplicate_count"] == 1


def test_cash_yield_reconciliation_computes_implied_monthly_yield():
    res = compute_cash_yield_reconciliation(
        previous_balance="10000.00",
        current_balance="12028.45",
        net_transfers="2000.00",
    )
    assert res["implied_yield"] == "28.45"
    assert res["needs_review"] is False

    neg = compute_cash_yield_reconciliation(
        previous_balance="10000.00",
        current_balance="9500.00",
        net_transfers="0",
    )
    assert neg["implied_yield"] == "-500.00"
    assert neg["needs_review"] is True


def test_account_maintenance_tiers():
    assert account_maintenance_tier("ibkr") == "auto"
    assert account_maintenance_tier("unemployment_insurance") == "none"
    assert account_maintenance_tier("huawei_stock") == "event"
    assert account_maintenance_tier("hpf") == "quarterly"
    assert account_maintenance_tier("ccb_7332") == "monthly"
    assert account_maintenance_tier("custom", {"include_in_nav": False}) == "none"
