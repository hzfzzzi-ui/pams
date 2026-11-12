from __future__ import annotations

import sys
import inspect
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest

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


@pytest.mark.parametrize("category", ["消费", "其他支出", "金融费用"])
def test_financial_expenses_group_without_changing_cashflow_direction(category):
    row = {"id": 1, "amount": -20, "amount_cny": -20, "classification_status": "confirmed",
           "category": category, "subcategory": "券商利息"}
    classified = {**row, **ledger_view._classify_cash_direction(row, set())}
    assert (classified["flow_type"], classified["category"], classified["subcategory"]) == (
        "expense", "金融费用", "券商利息",
    )
    assert ledger_view._group_amounts([classified], "expense") == [
        {"category": "金融费用", "amount": 20, "count": 1, "share": 1},
    ]
    assert ledger_view._classify_cash_direction(row, {1})["flow_type"] == "transfer"
    assert ledger_view._classify_cash_direction({**row, "amount": 20}, set())["coarse_category"] == "券商利息"


def test_stream_filters_before_paging_and_breaks_amount_ties_by_date():
    rows = [
        {'id': ident, 'account_code': account, 'flow_type': flow, 'summary': 'Coffee',
         'transaction_date': date(2026, 10, day), 'transaction_date_obj': date(2026, 10, day),
         'amount': amount, 'amount_cny': amount}
        for ident, account, flow, day, amount in [
            (1, 'bank', 'expense', 1, -10), (2, 'bank', 'expense', 2, -10),
            (3, 'bank', 'income', 3, 100), (4, 'other', 'expense', 4, -30),
            (5, 'bank_card', 'expense', 5, -20),
        ]
    ]
    account_config = {
        'account_roots': {'bank_group': {'display_name': '测试银行'}},
        'accounts': {
            'bank': {'parent_account_code': 'bank_group', 'display_name': '储蓄卡'},
            'bank_card': {'parent_account_code': 'bank_group', 'display_name': '信用卡'},
        },
    }
    filters = dict(account_config=account_config, base_currency='CNY', account='bank', flow='expense',
                   search='coffee', sort='amount_desc', limit=1)
    first = ledger_view._ledger_stream_page(rows, offset=0, **filters)
    second = ledger_view._ledger_stream_page(rows, offset=first['next_offset'], **filters)
    assert [page['rows'][0]['id'] for page in (first, second)] == [2, 1]
    assert second['next_offset'] is None
    alias = ledger_view._ledger_stream_page(rows, offset=0, matching_accounts={'bank'},
                                           **{**filters, 'search': '账户旧称'})
    assert alias['rows'][0]['id'] == 2
    group_filters = {**filters, 'account': '', 'account_group': 'bank_group'}
    page = ledger_view._ledger_stream_page(rows, offset=0, **group_filters)
    assert [row['id'] for row in page['rows']] == [5]
    assert page['account_groups'][0] == {
        'code': 'bank_group', 'label': '测试银行',
        'accounts': [{'code': 'bank', 'label': '储蓄卡'}, {'code': 'bank_card', 'label': '信用卡'}],
    }
    page = ledger_view._ledger_stream_page(rows, offset=page['next_offset'], **group_filters)
    assert [row['id'] for row in page['rows']] == [2]
    page = ledger_view._ledger_stream_page(rows, offset=page['next_offset'], **group_filters)
    assert [row['id'] for row in page['rows']] == [1]
    assert page['next_offset'] is None


def test_income_expense_stream_pages_only_direction_classified_rows():
    entries = [(4, 'transfer', -700), (3, 'investment_inflow', 700),
               (2, 'expense', -20), (1, 'income', 80)]
    rows = [
        {'id': ident, 'account_code': 'bank', 'flow_type': flow,
         'transaction_date': date(2026, 10, ident), 'transaction_date_obj': date(2026, 10, ident),
         'amount': amount, 'amount_cny': amount, 'category': '内部划转',
         'is_internal_transfer': flow == 'transfer'}
        for ident, flow, amount in entries
    ]
    filters = dict(account_config={}, base_currency='CNY', account='', search='', sort='', limit=1)
    first = ledger_view._ledger_stream_page(rows, offset=0, flow='income_expense', **filters)
    second = ledger_view._ledger_stream_page(rows, offset=first['next_offset'], flow='income_expense', **filters)
    assert [page['rows'][0]['id'] for page in (first, second)] == [2, 1]
    assert second['next_offset'] is None
    assert ledger_view._ledger_stream_page(rows, offset=0, flow='', **filters)['rows'][0]['id'] == 4

    cash_rows = [row for row in rows if row['flow_type'] != 'investment_inflow']
    all_filters = {**filters, 'limit': 10}
    stream = ledger_view._ledger_stream_page(cash_rows, offset=0, flow='cashflow', **all_filters)
    assert [row['id'] for row in stream['rows']] == [4, 2, 1]
    transfers = ledger_view._ledger_stream_page(cash_rows, offset=0, flow='cash_transfer', **all_filters)
    assert [(row['id'], row['amount']) for row in transfers['rows']] == [(4, -700)]
    another_account = ledger_view._ledger_stream_page(
        cash_rows, offset=0, flow='cash_transfer', **{**all_filters, 'account': 'other'},
    )
    assert another_account['rows'] == []


@pytest.mark.parametrize('flow', ['cashflow', 'cash_transfer'])
def test_transfer_cards_are_deduplicated_before_paging_and_keep_both_accounts(flow):
    day = date(2026, 10, 4)
    rows = [
        {'id': ident, 'account_code': account, 'transaction_date': day, 'transaction_date_obj': day,
         'flow_type': 'transfer', 'amount': amount, 'amount_cny': amount}
        for ident, account, amount in [(1, 'bank', -100), (2, 'card', 100)]
    ]
    match = {
        'id': 'pair-1', 'source_row_id': 1, 'target_row_id': 2, 'match_date': day,
        'from_account_code': 'bank', 'to_account_code': 'card', 'amount': 100, 'amount_cny': 100,
    }
    filters = dict(account_config={}, base_currency='CNY', flow=flow, search='', sort='', offset=0, limit=1)
    ordinary = ledger_view._ledger_stream_page(rows, account='', **filters)
    assert ordinary['next_offset'] == 1
    for account in ('', 'bank', 'card'):
        page = ledger_view._ledger_stream_page(rows, account=account, transfer_matches=[match], **filters)
        assert page['next_offset'] is None
        assert page['rows'] == [{'id': 'transfer-pair-pair-1', 'transaction_date': '2026-10-04', 'transfer_pair': match}]


def test_redemption_receipt_is_internal_transfer_on_arrival_day(monkeypatch):
    from domain.ledger.confirmed_transfers_v2 import REDEMPTION_PURPOSE
    from decimal import Decimal

    class Cursor:
        def execute(self, query, params):
            assert params['day'] == date(2026, 7, 21)
        def fetchone(self):
            return {'fx_rate': Decimal('1')}

    monkeypatch.setattr(ledger_view, '_daily_fx_join', lambda *args, **kwargs: '')
    day, arrival = date(2026, 7, 20), date(2026, 7, 21)
    left = {'kind': 'trade', 'id': 10, 'purpose': REDEMPTION_PURPOSE,
            'day': str(day), 'receipt_day': str(arrival)}
    right = {'kind': 'cashflow', 'id': 10, 'day': str(arrival)}
    item = {'transfer_id': 'pair', 'evidence_status': 'confirmed', 'currency': 'CNY',
            'from_account_code': 'fund', 'to_account_code': 'cash', 'amount': Decimal('25.20')}
    match, = ledger_view._load_transfer_matches(
        Cursor(), start=day, end=arrival, base_currency='CNY', cash_pairs_only=True,
        decisions=[(item, left, right, day, arrival, 'investment_withdrawal')],
    )
    assert (match['match_type'], match['match_date'], match['source_date']) == ('internal_transfer', arrival, day)

    from domain.investments import portfolio_state
    from domain.ledger import confirmed_transfers_v2
    monkeypatch.setattr(portfolio_state, '_table_exists', lambda *args: True)
    monkeypatch.setattr(confirmed_transfers_v2, 'read', lambda *args, **kwargs: [
        {**item, 'from_evidence': left, 'to_evidence': right},
    ])
    portfolio_match, = portfolio_state.load_transfer_matches(Cursor())
    assert (portfolio_match['match_type'], portfolio_match['match_date']) == ('internal_transfer', str(arrival))
    assert (match['source_row_id'], match['source_trade_id'], match['target_row_id']) == (None, 10, 10)
    match['match_date_obj'] = match['match_date']
    cash = {'id': 10, 'amount': Decimal('25.20'), 'amount_cny': 25.20, 'transaction_date_obj': arrival}
    cash.update(ledger_view._classify_cash_direction(cash, {10}))
    summary = summarize_period([cash], [match], day, arrival)
    assert summary['income'] == summary['expense'] == summary['investment_transfer_net'] == 0
    assert float(summary['internal_transfer']) == 25.20


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


def test_source_ledger_uses_original_direction_until_both_legs_are_matched():
    jan = date(2026, 1, 31)
    feb = date(2026, 2, 1)
    facts = [
        {"id": 1, "amount": -100, "amount_cny": -100, "transaction_date_obj": jan,
         "classification_status": "confirmed", "cashflow_type": "investment_funding"},
        {"id": 2, "amount": 100, "amount_cny": 100, "transaction_date_obj": feb,
         "classification_status": "pending"},
        {"id": 3, "amount": 20, "amount_cny": 20, "transaction_date_obj": feb,
         "classification_status": "confirmed", "cashflow_type": "operating_expense", "subcategory": "消费退款"},
        {"id": 4, "amount": 0, "amount_cny": 0, "transaction_date_obj": feb, "classification_status": "pending"},
    ]
    before = [dict(row, **ledger_view._classify_cash_direction(row, set())) for row in facts]
    assert [row["flow_type"] for row in before] == ["expense", "income", "income", "other"]
    assert summarize_period(before, [], jan, feb)["subcategory_pending_count"] == 2
    assert summarize_period(before, [], jan, feb)["income"] == 120
    assert summarize_period(before, [], jan, feb)["expense"] == 100

    after = [dict(row, **ledger_view._classify_cash_direction(row, {1, 2})) for row in facts]
    assert [row.get("account_flow_type") for row in after] == ["expense", "income", "income", None]
    assert summarize_period(after, [], jan, feb)["income"] == 20
    assert summarize_period(after, [], jan, feb)["expense"] == 0
    monthly = ledger_view._trend(after, jan, feb, "monthly")
    assert sum(point["income"] for point in monthly) == 20
    assert sum(point["expense"] for point in monthly) == 0


def test_transfer_reader_requires_confirmed_cash_legs_and_accepts_different_dates(monkeypatch):
    jan, feb = date(2026, 1, 31), date(2026, 2, 1)
    item = {"evidence_status": "confirmed", "currency": "CNY", "transfer_id": "pair", "amount": 100,
            "from_account_code": "bank", "to_account_code": "broker"}
    outflow = {"kind": "cashflow", "id": 1}
    inflow = {"kind": "cashflow", "id": 2}
    trade = {"kind": "trade", "id": 3}
    monkeypatch.setattr(ledger_view, "_confirmed_transfer_records", lambda *args, **kwargs: [
        (item, outflow, inflow, jan, feb, "internal_transfer"),
        (item, outflow, trade, jan, jan, "investment_funding"),
        ({**item, "evidence_status": "revoked"}, outflow, inflow, jan, jan, "internal_transfer"),
    ])
    monkeypatch.setattr(ledger_view, "_daily_fx_join", lambda *args, **kwargs: "")

    class Cursor:
        def execute(self, sql, params):
            assert params["day"] == jan

        def fetchone(self):
            return {"fx_rate": 1}

    matches = ledger_view._load_transfer_matches(Cursor(), start=jan, end=feb, base_currency="CNY", cash_pairs_only=True)
    assert [(match["source_row_id"], match["target_row_id"]) for match in matches] == [(1, 2)]


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
        {"match_date_obj": date(2026, 5, 5), "amount_cny": 300.0, "match_type": "investment_withdrawal"},
    ]

    summary = summarize_period(rows, transfers, date(2026, 5, 1), date(2026, 5, 31))

    assert summary["net"] == 1700.0
    assert summary["disposable_net"] == 1700.0
    assert summary["investment_funding"] == 1000.0
    assert summary["investment_transfer"] == 1300.0
    assert summary["investment_transfer_net"] == -700.0
    assert summary["other_transfer"] == 200.0
    assert summary["internal_transfer"] == 1500.0
    assert summary["cash_net"] == 1000.0

    transfers[0]["amount_cny"] = None
    missing_funding = summarize_period(rows, transfers, date(2026, 5, 1), date(2026, 5, 31))
    assert missing_funding["investment_funding"] is None


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
