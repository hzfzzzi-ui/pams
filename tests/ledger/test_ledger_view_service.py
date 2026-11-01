from __future__ import annotations

import sys
import inspect
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import domain.ledger.ledger_view as ledger_view  # noqa: E402
from domain.ledger.ledger_view import (  # noqa: E402
    _account_summary,
    _merge_investment_account_rows,
    _month_bounds,
    _recent_start_for_account_window,
    _serialize_row,
    classify_ledger_row,
    summarize_period,
)


def test_month_bounds_defaults_to_current_month_to_date(monkeypatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 6, 21)

    monkeypatch.setattr(ledger_view, "date", FixedDate)

    start, end = _month_bounds(None, latest_date=date(2026, 5, 31))

    assert start == date(2026, 6, 1)
    assert end == date(2026, 6, 21)


def test_month_bounds_keeps_explicit_historical_month(monkeypatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 6, 21)

    monkeypatch.setattr(ledger_view, "date", FixedDate)

    start, end = _month_bounds("2026-05")

    assert start == date(2026, 5, 1)
    assert end == date(2026, 5, 31)


def test_classify_transfer_before_expense_without_amount_threshold() -> None:
    row = {
        "id": 10,
        "amount_cny": -520,
        "category": "日常消费",
        "subcategory": "小额消费/红包",
        "description": "跨行代收 信用卡还款",
    }

    result = classify_ledger_row(row, transfer_row_ids=set())

    assert result["flow_type"] == "transfer"
    assert result["coarse_category"] == "资金流转"
    assert result["needs_review"] is False


def test_classify_expense_uses_coarse_merchant_category() -> None:
    row = {
        "id": 11,
        "amount_cny": -86.4,
        "category": "未分类",
        "summary": "信用卡交易",
        "description": "DLO*UBER RIDE CDMX MX",
    }

    result = classify_ledger_row(row)

    assert result["flow_type"] == "expense"
    assert result["coarse_category"] == "交通出行"
    assert result["needs_review"] is False


def test_payment_proxy_target_side_counts_as_expense_not_transfer() -> None:
    row = {
        "id": 866,
        "source_id": "486",
        "amount_cny": -441,
        "category": "支付通道",
        "subcategory": "银行卡扣款映射",
        "summary": "群收款",
        "description": "🥇yi 建设银行储蓄卡(7332) 支付成功",
        "counterparty": "🥇yi",
        "cashflow_type": "internal_transfer",
        "is_internal_transfer": True,
        "confirmed_payment_proxy_role": "target",
        "transfer_group_id": "payment_proxy:ccb_wechat_payment_proxy:1132:486:441.00",
    }

    result = classify_ledger_row(row, transfer_row_ids={486}, payment_proxy_source_row_ids={1132})

    assert result["flow_type"] == "expense"
    assert result["coarse_category"] == "人情往来"
    assert result["needs_review"] is False


def test_payment_proxy_source_side_is_dedup_transfer_row() -> None:
    row = {
        "id": 870,
        "source_id": "1132",
        "amount_cny": -441,
        "category": "未分类",
        "summary": "充值",
        "description": "充值 财付通-微信支付-群收款",
        "counterparty": "Z******0010/*收款",
        "cashflow_type": "internal_transfer",
        "is_internal_transfer": True,
        "confirmed_payment_proxy_role": "source",
        "transfer_group_id": "payment_proxy:ccb_wechat_payment_proxy:1132:486:441.00",
    }

    result = classify_ledger_row(row, transfer_row_ids=set(), payment_proxy_source_row_ids={1132})

    assert result["flow_type"] == "payment_proxy_dedup"
    assert result["coarse_category"] == "支付通道去重"
    assert result["needs_review"] is False


def test_benefit_asset_positive_row_counts_as_income() -> None:
    row = {
        "id": 877,
        "amount_cny": 3655,
        "category": "福利资产",
        "subcategory": "公积金汇缴",
        "summary": "汇缴",
        "description": "深圳住房公积金汇缴",
        "counterparty": "深圳市住房公积金管理中心",
        "cashflow_type": "operating_income",
        "is_internal_transfer": False,
    }

    result = classify_ledger_row(row, transfer_row_ids=set())

    assert result["flow_type"] == "income"
    assert result["coarse_category"] == "福利保障"
    assert result["needs_review"] is False
    assert result["is_operating"] is True


def test_benefit_asset_income_overrides_transfer_match() -> None:
    row = {
        "id": 1088,
        "amount_cny": 470,
        "category": "福利资产",
        "subcategory": "医保缴费",
        "summary": "医保个人账户入账",
        "description": "按缴存基数23500推算：个人医保进入账户470.00",
        "counterparty": "华为技术有限公司",
        "cashflow_type": "operating_income",
        "is_internal_transfer": False,
    }

    result = classify_ledger_row(row, transfer_row_ids={1088})

    assert result["flow_type"] == "income"
    assert result["coarse_category"] == "福利保障"
    assert result["needs_review"] is False
    assert result["is_operating"] is True


def test_income_categories_are_normalized_to_app_buckets() -> None:
    salary = classify_ledger_row(
        {
            "amount_cny": 26800,
            "cashflow_type": "operating_income",
            "category": "收入",
            "subcategory": "工资",
            "summary": "HUAWEI TECHNOLOGIES DE MEXICO SA DE CV",
        }
    )
    refund = classify_ledger_row(
        {
            "amount_cny": 1359,
            "cashflow_type": "operating_income",
            "category": "消费退款",
            "subcategory": "购物退款",
            "summary": "淘宝平台-退款",
        }
    )
    transfer_income = classify_ledger_row(
        {
            "amount_cny": 5000,
            "cashflow_type": "operating_income",
            "category": "往来款",
            "subcategory": "借款归还",
            "summary": "微信转账",
        }
    )
    year_end_bonus = classify_ledger_row(
        {
            "amount_cny": 37120,
            "cashflow_type": "operating_income",
            "category": "奖金",
            "subcategory": "年终奖",
            "summary": "普通汇兑",
            "description": "普通汇兑 分红（完税） 4000020319200082565/华为投资控股 有限公司工会委员会",
        }
    )

    assert salary["coarse_category"] == "工资薪酬"
    assert refund["coarse_category"] == "退款返现"
    assert transfer_income["coarse_category"] == "借还款/往来"
    assert year_end_bonus["flow_type"] == "income"
    assert year_end_bonus["coarse_category"] == "奖金"


def test_expense_categories_are_normalized_to_app_buckets() -> None:
    rent = classify_ledger_row(
        {
            "amount_cny": -25000,
            "cashflow_type": "operating_expense",
            "category": "房租",
            "summary": "TRANSFERENCIA A RENTA",
        }
    )
    phone = classify_ledger_row(
        {
            "amount_cny": -100,
            "cashflow_type": "operating_expense",
            "category": "日常消费",
            "description": "手机充值-中国电信 100元手机话费",
        }
    )
    people = classify_ledger_row(
        {
            "amount_cny": -1000,
            "cashflow_type": "operating_expense",
            "category": "未分类",
            "description": "TRANSFERENCIA A ZHANG G",
        }
    )

    assert rent["coarse_category"] == "居住"
    assert phone["coarse_category"] == "通讯网络"
    assert people["coarse_category"] == "人情往来"


def test_classify_unmatched_unclear_expense_needs_review() -> None:
    row = {
        "id": 12,
        "amount_cny": -1000,
        "category": "未分类",
        "description": "未知商户",
    }

    result = classify_ledger_row(row)

    assert result["flow_type"] == "expense"
    assert result["coarse_category"] == "待确认支出"
    assert result["needs_review"] is True


def test_summarize_period_uses_one_way_transfer_matches() -> None:
    rows = [
        {"transaction_date_obj": date(2026, 5, 1), "amount_cny": 2000.0, "flow_type": "income", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 2), "amount_cny": -300.0, "flow_type": "expense", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 3), "amount_cny": -1000.0, "flow_type": "transfer", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 3), "amount_cny": 1000.0, "flow_type": "transfer", "needs_review": False},
    ]
    transfers = [{"match_date_obj": date(2026, 5, 3), "amount_cny": 1000.0}]

    summary = summarize_period(rows, transfers, date(2026, 5, 1), date(2026, 5, 31))

    assert summary["income"] == 2000.0
    assert summary["expense"] == 300.0
    assert summary["net"] == 1700.0
    assert summary["internal_transfer"] == 1000.0


def test_missing_transfer_fx_does_not_erase_known_operating_income():
    day=date(2026,5,1)
    rows=[{'transaction_date_obj':day,'amount_cny':1000.0,'flow_type':'income','needs_review':False},
          {'transaction_date_obj':day,'amount_cny':None,'flow_type':'transfer','needs_review':False}]
    result=summarize_period(rows,[],day,day)
    assert result['income']==1000.0 and result['expense']==0 and result['net']==1000.0
    assert result['internal_transfer'] is None and result['cash_net'] is None
    assert result['pending_count']==1 and result['fx_missing_count']==1


def test_summarize_period_separates_disposable_and_cash_net() -> None:
    rows = [
        {"transaction_date_obj": date(2026, 5, 1), "amount_cny": 2000.0, "flow_type": "income", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 2), "amount_cny": -300.0, "flow_type": "expense", "needs_review": False},
    ]
    transfers = [
        {"match_date_obj": date(2026, 5, 3), "amount_cny": 1000.0, "match_type": "investment_funding"},
        {"match_date_obj": date(2026, 5, 4), "amount_cny": 200.0, "match_type": "account_transfer"},
    ]

    summary = summarize_period(rows, transfers, date(2026, 5, 1), date(2026, 5, 31))

    assert summary["net"] == 1700.0
    assert summary["disposable_net"] == 1700.0
    assert summary["investment_transfer"] == 1000.0
    assert summary["investment_transfer_net"] == -1000.0
    assert summary["other_transfer"] == 200.0
    assert summary["internal_transfer"] == 1200.0
    assert summary["cash_net"] == 700.0


def test_serialize_row_preserves_transfer_fields_for_frontend_filter() -> None:
    row = {
        "id": 21,
        "transaction_date": date(2026, 6, 21),
        "amount": -1000,
        "amount_cny": -1000,
        "flow_type": "transfer",
        "coarse_category": "资金流转",
        "needs_review": False,
        "cashflow_type": "investment_funding",
        "is_internal_transfer": True,
        "transfer_group_id": "case-1",
    }

    out = _serialize_row(row)

    assert out["cashflow_type"] == "investment_funding"
    assert out["is_internal_transfer"] is True
    assert out["transfer_group_id"] == "case-1"


def test_account_summary_groups_month_rows_by_account() -> None:
    rows = [
        {"account_code": "bank-a", "flow_type": "income", "amount_cny": 3000, "needs_review": False},
        {"account_code": "bank-a", "flow_type": "expense", "amount_cny": -800, "needs_review": True},
        {"account_code": "bank-a", "flow_type": "transfer", "amount_cny": -1000, "needs_review": False},
        {"account_code": "wallet-b", "flow_type": "expense", "amount_cny": -120, "needs_review": False},
    ]

    result = _account_summary(rows)
    by_account = {row["account_code"]: row for row in result}

    assert by_account["bank-a"]["income"] == 3000
    assert by_account["bank-a"]["expense"] == 800
    assert by_account["bank-a"]["transfer"] == 1000
    assert by_account["bank-a"]["net"] == 1200
    assert by_account["bank-a"]["row_count"] == 3
    assert by_account["bank-a"]["pending_count"] == 1
    assert by_account["wallet-b"]["expense"] == 120


def test_account_summary_reports_raw_row_activity() -> None:
    rows = [
        {"account_code": "ccb_7332", "flow_type": "transfer", "amount_cny": -1000, "needs_review": False},
        {"account_code": "ccb_7332", "flow_type": "transfer", "amount_cny": 300, "needs_review": False},
    ]

    result = _account_summary(rows)

    assert result[0]["account_code"] == "ccb_7332"
    assert result[0]["row_count"] == 2
    assert result[0]["transfer"] == 1300
    assert result[0]["net"] == -700


def test_account_summary_reports_latest_sync_at() -> None:
    rows = [
        {
            "account_code": "ccb_7332",
            "flow_type": "transfer",
            "amount_cny": -1000,
            "needs_review": False,
            "imported_at": datetime(2026, 5, 31, 4, 42, 24),
        },
        {
            "account_code": "ccb_7332",
            "flow_type": "transfer",
            "amount_cny": 300,
            "needs_review": False,
            "imported_at": datetime(2026, 6, 2, 8, 30, 0),
        },
    ]

    result = _account_summary(rows)

    assert result[0]["latest_sync_at"] == "2026-06-02T08:30:00"


def test_recent_start_includes_full_account_month_for_long_month() -> None:
    assert _recent_start_for_account_window(date(2026, 5, 1), date(2026, 5, 31), 30) == date(2026, 5, 1)


def test_recent_start_preserves_trailing_window_when_it_starts_earlier() -> None:
    assert _recent_start_for_account_window(date(2026, 2, 1), date(2026, 2, 28), 30) == date(2026, 1, 30)


def test_investment_account_rows_keep_original_currency_amount_for_display() -> None:
    source = inspect.getsource(ledger_view._load_investment_account_rows)
    trade_source = inspect.getsource(ledger_view._load_investment_trade_account_rows)

    assert "ABS(cf.amount) AS amount" in source
    assert "pams_investment_trade_fact" in trade_source
    assert "'investment_outflow'" in trade_source
    assert "cf.transfer_group_id LIKE 'investment_funding:%%'" in source
    assert "account_transfer:%%" not in source
    assert "linked_cashflow_id" in source
    assert "_merge_investment_account_rows(rows, trade_rows)" in source
    assert "amount_cny AS amount" not in source
    assert "amount_cny AS amount" not in trade_source


def test_investment_account_rows_prefer_bank_cashflow_over_importer_cash_event() -> None:
    cash_row = {
        "id": "investment-inflow-855",
        "account_code": "tonghuashun",
        "flow_type": "investment_inflow",
        "transaction_date_obj": date(2026, 5, 20),
        "amount_cny": 50000,
        "counterparty": "ccb_7332",
        "linked_cashflow_id": "855",
    }
    trade_row = {
        "id": "investment-trade-259",
        "account_code": "tonghuashun",
        "flow_type": "investment_inflow",
        "transaction_date_obj": date(2026, 5, 20),
        "amount_cny": 50000,
        "counterparty": "tradingview",
        "linked_cashflow_id": None,
    }
    unmatched_trade_row = {
        "id": "investment-trade-98",
        "account_code": "tonghuashun",
        "flow_type": "investment_inflow",
        "transaction_date_obj": date(2026, 1, 29),
        "amount_cny": 100000,
        "counterparty": "tradingview",
        "linked_cashflow_id": None,
    }

    result = _merge_investment_account_rows([cash_row], [trade_row, unmatched_trade_row])

    assert {row["id"] for row in result} == {"investment-inflow-855", "investment-trade-259", "investment-trade-98"}
    assert next(row for row in result if row['id'] == 'investment-inflow-855')["counterparty"] == "ccb_7332"
    linked = dict(trade_row, linked_cashflow_id='855')
    assert {row['id'] for row in _merge_investment_account_rows([cash_row], [linked, unmatched_trade_row])} == {
        'investment-inflow-855', 'investment-trade-98'}


def test_account_summary_filters_unconfigured_accounts_when_provided() -> None:
    rows = [
        {"account_code": "ccb_7332", "flow_type": "transfer", "amount_cny": -1000, "needs_review": False},
        {"account_code": "jianhang", "flow_type": "transfer", "amount_cny": -188.7, "needs_review": False},
    ]

    result = _account_summary(rows, {"ccb_7332"})

    assert [row["account_code"] for row in result] == ["ccb_7332"]


def test_account_summary_separates_investment_account_inflows() -> None:
    rows = [
        {"account_code": "ibkr", "account_kind": "investment", "flow_type": "investment_inflow", "amount_cny": 1000, "needs_review": False},
    ]

    result = _account_summary(rows, {"ibkr"})

    assert result[0]["account_kind"] == "investment"
    assert result[0]["income"] == 0
    assert result[0]["expense"] == 0
    assert result[0]["investment_inflow"] == 1000
    assert result[0]["investment_outflow"] == 0
    assert result[0]["investment_return"] == 0
    assert result[0]["investment_fee"] == 0
    assert result[0]["net"] == 1000


def test_account_summary_uses_personal_capital_for_co_owned_investment_account() -> None:
    rows = [
        {
            "account_code": "tonghuashun",
            "account_kind": "investment",
            "flow_type": "investment_inflow",
            "amount_cny": 300000,
            "needs_review": False,
        },
    ]
    account_config = {
        "defaults": {"owner_ratio": 1},
        "accounts": {
            "tonghuashun": {
                "owner_ratio": 0.625,
                "capital_base": 400000,
                "personal_capital": 250000,
            }
        },
    }

    result = _account_summary(rows, {"tonghuashun"}, account_config)

    assert result[0]["investment_inflow"] == 250000
    assert result[0]["net"] == 250000
    assert result[0]["owner_ratio"] == 0.625
    assert result[0]["capital_base"] == 400000
    assert result[0]["personal_capital"] == 250000
    assert result[0]["funding_scope"] == "personal_capital"
    assert result[0]["row_count"] == 1


def test_summarize_period_ignores_investment_account_cashflows() -> None:
    rows = [
        {"transaction_date_obj": date(2026, 5, 1), "amount_cny": 2000, "flow_type": "income", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 2), "amount_cny": -500, "flow_type": "expense", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 3), "amount_cny": 3000, "flow_type": "investment_inflow", "needs_review": False},
    ]

    summary = summarize_period(rows, [], date(2026, 5, 1), date(2026, 5, 31))

    assert summary["income"] == 2000
    assert summary["expense"] == 500
    assert summary["net"] == 1500
